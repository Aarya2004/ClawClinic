# ClawClinic — Voice Integration

Voice receptionist for **Downtown Dental Toronto**, powered by ElevenLabs Conversational AI + a thin Python proxy in front of [Hermes Agent](https://hermes-agent.nousresearch.com/).

A patient calls a Twilio number → ElevenLabs handles speech-to-text and text-to-speech → its Custom LLM is pointed at this proxy → the proxy injects the ClawClinic persona, forwards the OpenAI-style chat-completion request to a local Hermes API server, and streams the answer back. The proxy also exposes two REST endpoints that ElevenLabs calls as **server tools** to book appointments and check / update their status. Bookings are persisted to a flat CSV file.

## Architecture

```
phone call
   │
Twilio number ─▶ ElevenLabs Agent ─┬─▶ Custom LLM   POST /v1/chat/completions ─▶ voice_proxy.py ─▶ Hermes (127.0.0.1:8642)
                                   │
                                   ├─▶ Tool        POST /book                  ─▶ voice_proxy.py ─▶ bookings.csv
                                   │
                                   └─▶ Tool        POST /appointment_status    ─▶ voice_proxy.py ─▶ bookings.csv
```

All of the proxy lives in [`voice_proxy.py`](./voice_proxy.py) — stdlib only, no dependencies. It listens on `127.0.0.1:8643` and is exposed to ElevenLabs via an ngrok tunnel.

## Why the proxy exists

1. **Persona injection.** ElevenLabs prepends a generic "You are an AI agent…" system message. The proxy strips it and substitutes the ClawClinic voice persona, which is tuned for short, phone-friendly replies.
2. **Cascade-timeout safety net.** Hermes' first-token latency is 3–5 s with a large system prompt. ElevenLabs' `cascade_timeout_seconds` defaults to 8 s. The proxy emits an immediate `assistant` role chunk so the SSE channel is alive, and optionally emits a short filler phrase if upstream is slow (disabled by default since the cascade timeout is raised to 15 s on the ElevenLabs side).
3. **Tool endpoints.** Booking and status updates need somewhere to live; folding them into the same process means a single ngrok tunnel covers everything.

## Endpoints

### `POST /v1/chat/completions`
OpenAI-compatible streaming chat completions. ElevenLabs Custom LLM points here. All `system` messages from ElevenLabs are replaced with the ClawClinic persona before forwarding to Hermes.

### `POST /book`
Create an appointment. Body:
```json
{
  "slot_start": "2026-05-27T09:00:00",
  "patient_name": "Jane Doe",
  "service": "Cleaning",
  "caller_id": "+14165550123",
  "notes": "first-time patient"
}
```
- `slot_start` must be ISO 8601. Duration is derived from `service` (cleaning 30 min, consultation 30 min, whitening 60 min, filling 45 min, extraction 60 min).
- Returns `{ok: true, confirmation: "BK-XXXXXXXX", ...}` on success.
- Returns `{ok: false, error: "slot_taken", ...}` if the requested window overlaps an existing non-cancelled booking.

### `POST /appointment_status`
Look up or update an appointment.
- Lookup by `confirmation` → single row.
- Lookup by `caller_id` → all rows for that number, future-first.
- Update: pass `confirmation` + `new_status` ∈ `{booked, confirmed, completed, cancelled, no_show}`.

### `GET /bookings`
Dump the full CSV as JSON. Useful for the demo.

### `GET /health`
Liveness probe.

## Local setup

### Prereqs
- macOS or Linux with Python 3.11+
- A running Hermes Agent gateway with the API server enabled. In `~/.hermes/.env`:
  ```bash
  API_SERVER_ENABLED=true
  API_SERVER_PORT=8642
  API_SERVER_HOST=127.0.0.1
  API_SERVER_KEY=<some-bearer-token>
  ```
  then `hermes gateway restart`.
- [ngrok](https://ngrok.com/) for exposing the proxy to ElevenLabs.

### Run
```bash
./run.sh           # proxy only (foreground, port 8643)
./run.sh tunnel    # proxy + ngrok on :8643
```

Smoke test:
```bash
curl -s http://127.0.0.1:8643/health
curl -s -X POST http://127.0.0.1:8643/book \
  -H "Content-Type: application/json" \
  -d '{"slot_start":"2026-05-27T09:00:00","patient_name":"Test","service":"Cleaning"}'
curl -s http://127.0.0.1:8643/bookings | python3 -m json.tool
```

## ElevenLabs configuration

1. **Conversational AI → Create Agent**.
2. **LLM section → Custom LLM**:
   - Server URL: `https://<your-ngrok>.ngrok-free.dev/v1/chat/completions`
   - Model name: `hermes-agent`
   - API key: the `API_SERVER_KEY` from `~/.hermes/.env`
   - System prompt: **leave empty** (the proxy injects its own)
3. **Advanced → cascade_timeout_seconds**: set to `15` (max). Hermes' TTF is 3–5 s and we want safety margin.
4. **Tools tab → Add Tool** (server tool, webhook):

   **`claw_clinic_book_appointment`** — `POST` to `/book`
   | Field | Required | Type | Source |
   |---|---|---|---|
   | `slot_start` | yes | string | LLM |
   | `patient_name` | yes | string | LLM |
   | `service` | no | enum (Cleaning, Consultation, Whitening, Filling, Extraction) | LLM |
   | `caller_id` | no | string | **Dynamic variable** `system__caller_id` |
   | `notes` | no | string | LLM |

   **`claw_clinic_appointment_status`** — `POST` to `/appointment_status`
   | Field | Required | Type | Source |
   |---|---|---|---|
   | `confirmation` | no | string | LLM |
   | `caller_id` | no | string | **Dynamic variable** `system__caller_id` |
   | `new_status` | no | enum (confirmed, completed, cancelled, no_show) | LLM |

5. **Phone tab → pair a Twilio number** (Account SID + Auth Token + the number).

## Files

| Path | Purpose |
|---|---|
| `voice_proxy.py` | The proxy. Single file, stdlib only. |
| `run.sh` | Convenience launcher. |
| `.gitignore` | Excludes runtime artifacts (csv, logs, env). |

## Tuning

In `voice_proxy.py`:
- `FILLER_PROBABILITY` — chance of speaking one filler on a slow turn; `0` disables it.
- `FILLER_DELAY_RANGE_SECONDS` — random delay window before the filler is used.
- `CLAWCLINIC_SYSTEM` — the persona string. Edit slot times, service list, hours, etc.
- `SERVICE_DURATIONS_MIN` — per-service appointment length.
- `VALID_STATUSES` — allowed status values.

## Known limitations

- **Single clinic, hardcoded.** Slot menu and clinic facts live inside the persona string. Multi-clinic support would mean reading these from a config file keyed by some agent or call attribute.
- **No timezone.** All datetimes are naive ISO strings. Fine for a single-clinic demo; would need explicit timezones for a real deployment.
- **CSV is the source of truth.** No database, no concurrency control beyond append-mostly writes. Two simultaneous booking requests for the same slot can technically race; the overlap check is read-then-write without a lock.
- **ngrok URL is ephemeral on free tier.** A restart issues a new URL and ElevenLabs has to be updated. For demo days this is fine.
