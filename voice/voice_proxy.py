"""ClawClinic voice proxy — stdlib only.

Sits between an ElevenLabs Conversational AI agent and a local Hermes
Agent gateway. Three jobs:

1. /v1/chat/completions — OpenAI-style streaming chat completion. Strips
   the ElevenLabs-supplied system message, injects the ClawClinic persona,
   forwards to Hermes, and streams the SSE response back. An immediate
   role chunk keeps the SSE channel alive so ElevenLabs' cascade timer
   does not kill us during Hermes' 3-5s time-to-first-token. On some slow
   turns, the proxy may emit one natural filler phrase so the caller does
   not sit in silence.

2. /book — server tool for the ElevenLabs agent. Persists appointments
   to a CSV with overlap detection. Slot durations are derived from the
   service type.

3. /appointment_status — server tool for lookup or update. Lookup by
   confirmation number returns a single row; lookup by caller_id returns
   every row for that phone, future-first. Status updates are restricted
   to a fixed set (booked / confirmed / completed / cancelled / no_show).
   Cancelled bookings free their slot.

See README.md for ElevenLabs configuration.
"""

import csv
from collections import defaultdict, deque
import json
import os
import queue
import random
import socket
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERMES_URL = "http://127.0.0.1:8642/v1/chat/completions"
PORT = 8643

# Booking log — appended to as a CSV the user can open in Excel / Numbers / Sheets.
BOOKINGS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookings.csv")
BOOKING_FIELDS = [
    "timestamp",
    "confirmation",
    "status",
    "slot_start",
    "slot_end",
    "service",
    "patient_name",
    "caller_id",
    "notes",
]

VALID_STATUSES = {"booked", "confirmed", "completed", "cancelled", "no_show"}

SERVICE_DURATIONS_MIN = {
    "cleaning": 30,
    "consultation": 30,
    "consult": 30,
    "whitening": 60,
    "checkup": 30,
    "filling": 45,
    "extraction": 60,
}
DEFAULT_DURATION_MIN = 30

MAX_BODY_BYTES = 256 * 1024
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMITS = {
    "chat": 90,
    "tool": 180,
    "read": 240,
}

BOOKINGS_LOCK = threading.RLock()
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_BUCKETS: dict[tuple[str, str], deque[float]] = defaultdict(deque)


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_iso(s: str):
    """Parse ISO 8601. Accept trailing 'Z' as UTC. Returns datetime or None."""
    from datetime import datetime
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def service_duration_min(service: str) -> int:
    return SERVICE_DURATIONS_MIN.get((service or "").strip().lower(), DEFAULT_DURATION_MIN)


def append_booking(row: dict) -> None:
    with BOOKINGS_LOCK:
        os.makedirs(os.path.dirname(BOOKINGS_CSV), exist_ok=True)
        new_file = not os.path.exists(BOOKINGS_CSV)
        with open(BOOKINGS_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=BOOKING_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in BOOKING_FIELDS})


def read_all_bookings() -> list:
    with BOOKINGS_LOCK:
        if not os.path.exists(BOOKINGS_CSV):
            return []
        try:
            with open(BOOKINGS_CSV, newline="") as f:
                return list(csv.DictReader(f))
        except (OSError, csv.Error) as e:
            print(f"[booking] read error: {e}", flush=True)
            return []


def write_all_bookings(rows: list) -> None:
    with BOOKINGS_LOCK:
        os.makedirs(os.path.dirname(BOOKINGS_CSV), exist_ok=True)
        tmp = BOOKINGS_CSV + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=BOOKING_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in BOOKING_FIELDS})
        os.replace(tmp, BOOKINGS_CSV)


def make_confirmation() -> str:
    return f"BK-{uuid.uuid4().hex[:8].upper()}"


def unwrap_tool_payload(payload: dict) -> dict:
    """Accept common webhook/tool argument wrappers without failing the call."""
    if not isinstance(payload, dict):
        return {}
    for key in ("parameters", "arguments", "args", "input", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
    return payload

# Emit a short spoken filler on some slow turns. The delay is jittered and
# the probability keeps the voice experience from sounding scripted.
FILLER_PROBABILITY = 0.45  # 0 = disabled, 1 = always on slow turns
FILLER_DELAY_RANGE_SECONDS = (2.2, 4.2)
FILLERS = [
    "One moment while I check that. ",
    "Let me check that for you. ",
    "Just a second while I look that up. ",
    "I’m checking the schedule now. ",
    "Give me a moment to pull that up. ",
    "I’ll take a quick look. ",
    "Let me see what’s available. ",
    "I’m looking into that now. ",
]

CLAWCLINIC_SYSTEM = """You are ClawClinic, the AI voice receptionist for Downtown Dental Toronto.

PERSONA:
- Friendly, calm, professional dental front-desk voice.
- Brief replies (1-3 sentences). Speak naturally for phone — no lists, no markdown.
- You are NOT Claude, NOT a general assistant. If asked who you are, say: "I'm ClawClinic, the AI receptionist at Downtown Dental Toronto."
- Never give medical advice. For symptoms, suggest the patient book a visit or contact emergency services.

CLINIC FACTS (use these exact values):
- Clinic: Downtown Dental Toronto
- Hours: Monday to Friday 8 AM to 6 PM, Saturday 9 AM to 2 PM, closed Sunday
- Address: 123 King Street West, Toronto, Ontario
- Phone bookings: collect name and call-back number, then say you will send an SMS link to confirm
- Insurance accepted: Sun Life, Manulife, Canada Life, Green Shield, Pacific Blue Cross
- Booking fee to the clinic: paid via x402 on GOAT Network (do not mention this unless asked)

AVAILABLE SLOTS for booking (today is 2026-05-26):
1. 2026-05-27T09:00:00 — Cleaning (30 min)
2. 2026-05-27T10:30:00 — Cleaning (30 min)
3. 2026-05-27T13:00:00 — Consultation (30 min)
4. 2026-05-28T09:00:00 — Cleaning (30 min)
5. 2026-05-28T11:00:00 — Whitening (60 min)
6. 2026-05-28T14:00:00 — Consultation (30 min)

When speaking to the caller, refer to slots in natural language (e.g. "tomorrow at 9 AM" or "Thursday afternoon"). When calling the booking tool, pass the slot as a full ISO 8601 datetime like 2026-05-27T09:00:00.

BEHAVIOR FOR VOICE CALLS:
- At the start of a new call, greet the caller first: "Hi, this is ClawClinic, the AI receptionist for Downtown Dental Toronto. I can help book appointments, check hours, answer insurance questions, or look up an existing booking. How can I help today?"
- If asked about hours, give the hours directly.
- If asked about insurance, list a few accepted providers and confirm if theirs is covered.
- If they want to BOOK an appointment:
  1. Read 2-3 available slots in natural language, ask which they prefer.
  2. Ask for their full name.
  3. Call the claw_clinic_book_appointment tool with slot_start (ISO 8601), patient_name, and service (Cleaning / Consultation / Whitening). The caller's phone number is captured automatically — do NOT ask for it.
  4. The tool returns a confirmation number like BK-XXXXXXXX. Read it back to the caller.
  5. If the tool says the slot is already taken, apologise and offer one of the other available times.
- If they want to CANCEL or CHECK STATUS:
  - Ask if they have their confirmation number (BK-XXXXXXXX).
  - If yes, call claw_clinic_appointment_status with that confirmation.
  - If they don't have it, call claw_clinic_appointment_status with just caller_id (auto-filled from their phone number) to look up their bookings, then read back what you find.
  - To cancel, call the same tool with confirmation + new_status="cancelled".
- If asked who built you or what tech: "I'm an on-chain AI agent built by MedPortAI for OpenClaw Hack Toronto."
- Keep every response under 30 words when possible. This is a phone call.
"""



def sse_chunk(chat_id: str, created: int, model: str, delta: dict, finish=None) -> bytes:
    obj = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(obj)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[proxy] {self.address_string()} - {fmt % args}", flush=True)

    def _json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _endpoint_kind(self, path: str) -> str:
        if path == "/v1/chat/completions":
            return "chat"
        if path in ("/book", "/appointment_status"):
            return "tool"
        return "read"

    def _rate_limited(self, path: str) -> bool:
        if path in ("/health", "/v1/health"):
            return False
        kind = self._endpoint_kind(path)
        limit = RATE_LIMITS[kind]
        key = (self.client_address[0], kind)
        now = time.monotonic()
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        with RATE_LIMIT_LOCK:
            bucket = RATE_LIMIT_BUCKETS[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return True
            bucket.append(now)
        return False

    def _read_json_body(self) -> dict:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise RequestError(411, "Invalid Content-Length")
        if length < 0:
            raise RequestError(411, "Invalid Content-Length")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise RequestError(413, "Request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception as e:
            raise RequestError(400, "Invalid JSON") from e
        if not isinstance(payload, dict):
            raise RequestError(400, "JSON body must be an object")
        return payload

    def _safe_json_error(self, status: int, message: str) -> None:
        try:
            self._json(status, {"ok": False, "error": message})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            print(f"[proxy] unhandled GET error: {e}", flush=True)
            self._safe_json_error(500, "internal error")

    def _do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if self._rate_limited(path):
            return self._json(429, {"ok": False, "error": "rate_limited"})
        if path in ("/health", "/v1/health"):
            return self._json(200, {"status": "ok", "proxy": "clawclinic-voice"})
        if path == "/bookings":
            rows = read_all_bookings()
            return self._json(200, {"count": len(rows), "bookings": rows})
        self.send_error(404)

    def handle_booking(self):
        from datetime import timedelta

        try:
            payload = self._read_json_body()
        except RequestError:
            return self._json(
                200,
                {
                    "ok": True,
                    "status": "needs_more_info",
                    "message": "I had trouble reading the booking details. Please ask the caller for the time and name again.",
                },
            )
        payload = unwrap_tool_payload(payload)

        slot_start_raw = (payload.get("slot_start") or "").strip()
        name = (payload.get("patient_name") or "").strip()
        service = (payload.get("service") or "Cleaning").strip()
        caller_id = (payload.get("caller_id") or "").strip()
        notes = (payload.get("notes") or "").strip()

        if not slot_start_raw or not name:
            return self._json(
                200,
                {
                    "ok": True,
                    "status": "needs_more_info",
                    "missing": [
                        *([] if slot_start_raw else ["slot_start"]),
                        *([] if name else ["patient_name"]),
                    ],
                    "message": "I need both a slot start time and a patient name to book. Ask for the missing detail, then try again.",
                },
            )

        start = parse_iso(slot_start_raw)
        if start is None:
            return self._json(
                200,
                {
                    "ok": True,
                    "status": "needs_more_info",
                    "message": (
                        "I couldn't parse that time. Please use an ISO 8601 datetime "
                        "like 2026-05-27T09:00:00."
                    ),
                },
            )

        dur = service_duration_min(service)
        end = start + timedelta(minutes=dur)
        slot_start_iso = start.isoformat()
        slot_end_iso = end.isoformat()

        with BOOKINGS_LOCK:
            # Overlap detection: existing.start < new.end AND existing.end > new.start
            # Cancelled bookings free the slot.
            if os.path.exists(BOOKINGS_CSV):
                try:
                    existing_rows = read_all_bookings()
                except Exception as e:
                    print(f"[booking] overlap read error: {e}", flush=True)
                    existing_rows = []
                for row in existing_rows:
                    if (row.get("status") or "").lower() == "cancelled":
                        continue
                    ex_start = parse_iso(row.get("slot_start", ""))
                    ex_end = parse_iso(row.get("slot_end", ""))
                    if not ex_start or not ex_end:
                        continue
                    if ex_start < end and ex_end > start:
                        return self._json(
                            200,
                            {
                                "ok": True,
                                "status": "slot_taken",
                                "conflicting_slot_start": row.get("slot_start"),
                                "conflicting_slot_end": row.get("slot_end"),
                                "message": (
                                    f"That time conflicts with an existing booking "
                                    f"from {row.get('slot_start')} to {row.get('slot_end')}. "
                                    "Please pick a different time."
                                ),
                            },
                        )

            confirmation = make_confirmation()
            row = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "confirmation": confirmation,
                "status": "booked",
                "slot_start": slot_start_iso,
                "slot_end": slot_end_iso,
                "service": service,
                "patient_name": name,
                "caller_id": caller_id,
                "notes": notes,
            }
            append_booking(row)
        print(
            f"[booking] {confirmation} | {slot_start_iso} → {slot_end_iso} | "
            f"{service} | {name} | {caller_id}",
            flush=True,
        )
        return self._json(
            200,
            {
                "ok": True,
                "status": "booked",
                "confirmation": confirmation,
                "slot_start": slot_start_iso,
                "slot_end": slot_end_iso,
                "service": service,
                "patient_name": name,
                "message": (
                    f"Booked {service} for {name} from {slot_start_iso} to {slot_end_iso}. "
                    f"Confirmation number {confirmation}."
                ),
            },
        )

    def handle_status(self):
        """Look up or update appointment(s).

        Lookup modes:
          - by `confirmation` (single appointment)
          - by `caller_id` (all appointments for that number, future bookings first)

        Update mode requires `confirmation` + `new_status` ∈ VALID_STATUSES.
        """
        try:
            payload = self._read_json_body()
        except RequestError as e:
            return self._json(e.status, {"ok": False, "error": e.message})

        confirmation = (payload.get("confirmation") or "").strip().upper()
        caller_id = (payload.get("caller_id") or "").strip()
        new_status = (payload.get("new_status") or "").strip().lower()

        if not confirmation and not caller_id:
            return self._json(
                200,
                {
                    "ok": False,
                    "error": "missing_lookup",
                    "message": (
                        "I need either a confirmation number or a phone number "
                        "to look up an appointment."
                    ),
                },
            )

        with BOOKINGS_LOCK:
            rows = read_all_bookings()

            # Update path always requires a confirmation
            if new_status:
                return self._handle_status_update(rows, confirmation, new_status)

        # Lookup by confirmation — single row
        if confirmation:
            target = next(
                (r for r in rows if (r.get("confirmation") or "").upper() == confirmation),
                None,
            )
            if target is None:
                return self._json(
                    200,
                    {
                        "ok": False,
                        "error": "not_found",
                        "confirmation": confirmation,
                        "message": f"No booking found for {confirmation}.",
                    },
                )
            return self._json(200, {"ok": True, **target})

        # Lookup by caller_id — return all matching, future-first
        matches = [r for r in rows if (r.get("caller_id") or "") == caller_id]
        if not matches:
            return self._json(
                200,
                {
                    "ok": False,
                    "error": "not_found",
                    "caller_id": caller_id,
                    "message": f"No appointments found for that phone number.",
                },
            )

        # Sort: future-first, then by start time
        def sort_key(r):
            s = parse_iso(r.get("slot_start", ""))
            return (s is None, s)

        matches.sort(key=sort_key)
        return self._json(
            200,
            {
                "ok": True,
                "caller_id": caller_id,
                "count": len(matches),
                "appointments": matches,
            },
        )

    def _handle_status_update(self, rows: list, confirmation: str, new_status: str):
        if not confirmation:
            return self._json(
                200,
                {
                    "ok": False,
                    "error": "missing_confirmation",
                    "message": "To change status I need the confirmation number.",
                },
            )
        if new_status not in VALID_STATUSES:
            return self._json(
                200,
                {
                    "ok": False,
                    "error": "bad_status",
                    "message": (
                        "Status must be one of: "
                        f"{', '.join(sorted(VALID_STATUSES))}."
                    ),
                },
            )
        target = next(
            (r for r in rows if (r.get("confirmation") or "").upper() == confirmation),
            None,
        )
        if target is None:
            return self._json(
                200,
                {
                    "ok": False,
                    "error": "not_found",
                    "confirmation": confirmation,
                    "message": f"No booking found for {confirmation}.",
                },
            )
        old_status = target.get("status") or "booked"
        target["status"] = new_status
        write_all_bookings(rows)
        print(
            f"[booking] {confirmation} status {old_status} -> {new_status}",
            flush=True,
        )
        return self._json(
            200,
            {
                "ok": True,
                "confirmation": confirmation,
                "old_status": old_status,
                "status": new_status,
                "message": f"Appointment {confirmation} updated to {new_status}.",
            },
        )

    def do_POST(self):
        try:
            self._do_POST()
        except RequestError as e:
            self._safe_json_error(e.status, e.message)
        except Exception as e:
            print(f"[proxy] unhandled POST error: {e}", flush=True)
            self._safe_json_error(500, "internal error")

    def _do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if self._rate_limited(path):
            return self._json(429, {"ok": False, "error": "rate_limited"})
        if path == "/book":
            return self.handle_booking()
        if path == "/appointment_status":
            return self.handle_status()
        if path != "/v1/chat/completions":
            self.send_error(404)
            return

        payload = self._read_json_body()

        auth = self.headers.get("Authorization", "")
        payload["stream"] = True

        # Replace ElevenLabs' generic "You are an AI agent..." system message
        # with the ClawClinic persona. Keep all non-system messages intact.
        msgs = payload.get("messages") or []
        if not isinstance(msgs, list):
            msgs = []
        non_system = [
            m for m in msgs
            if isinstance(m, dict) and m.get("role") != "system"
        ]
        payload["messages"] = [{"role": "system", "content": CLAWCLINIC_SYSTEM}] + non_system

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        model = payload.get("model", "hermes-agent")

        # Headers for SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_sse(b: bytes):
            # chunked encoding: <hexlen>\r\n<data>\r\n
            self.wfile.write(f"{len(b):X}\r\n".encode())
            self.wfile.write(b)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        def end_chunked():
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        # 1) Immediate role chunk so the SSE channel is alive
        write_sse(sse_chunk(chat_id, created, model, {"role": "assistant"}))

        # Pump upstream into a queue from a worker thread so we can race it
        # against a filler timer.
        events: "queue.Queue" = queue.Queue()
        DONE = object()

        def upstream_worker():
            req = urllib.request.Request(
                HERMES_URL,
                data=json.dumps(payload).encode(),
                headers={"Authorization": auth, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as upstream:
                    buf = b""
                    while True:
                        chunk = upstream.read1(4096) if hasattr(upstream, "read1") else upstream.read(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            line = line.rstrip(b"\r")
                            if not line or not line.startswith(b"data:"):
                                continue
                            events.put(line)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")[:200]
                print(f"[proxy] upstream HTTPError {e.code}: {err_body}", flush=True)
                events.put(("ERROR", "I'm having trouble right now."))
            except Exception as e:
                print(f"[proxy] upstream error: {e}", flush=True)
                events.put(("ERROR", "I'm having trouble right now."))
            finally:
                events.put(DONE)

        t = threading.Thread(target=upstream_worker, daemon=True)
        t.start()

        sent_content_yet = False
        filler_emitted = False
        should_emit_filler = random.random() < FILLER_PROBABILITY
        filler_delay = random.uniform(*FILLER_DELAY_RANGE_SECONDS)
        start = time.time()

        try:
            while True:
                # On a sampled subset of slow turns, race upstream against a
                # jittered filler budget. Otherwise block until Hermes speaks.
                if should_emit_filler and not sent_content_yet and not filler_emitted:
                    timeout = max(0.05, filler_delay - (time.time() - start))
                else:
                    timeout = None

                try:
                    item = events.get(timeout=timeout)
                except queue.Empty:
                    # Upstream still hasn't produced content. Emit filler now.
                    filler = random.choice(FILLERS)
                    write_sse(sse_chunk(chat_id, created, model, {"content": filler}))
                    filler_emitted = True
                    continue

                if item is DONE:
                    break

                if isinstance(item, tuple) and item[0] == "ERROR":
                    write_sse(sse_chunk(chat_id, created, model, {"content": item[1]}, finish="stop"))
                    break

                # Item is a raw SSE 'data: ...' line from upstream
                line = item
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                obj["id"] = chat_id
                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    # Skip upstream's role-only chunk
                    if "role" in delta and "content" not in delta and not choices[0].get("finish_reason"):
                        continue
                    if delta.get("content"):
                        sent_content_yet = True
                write_sse(f"data: {json.dumps(obj)}\n\n".encode())

            write_sse(b"data: [DONE]\n\n")
            end_chunked()
        except Exception as e:
            print(f"[proxy] downstream write error: {e}", flush=True)


class VoiceHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = VoiceHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"voice proxy listening on http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
