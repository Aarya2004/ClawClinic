"""PharmaSupply — local A2A supplier agent.

A second standalone HTTP service that ClawClinic delegates procurement to.
Two endpoints:

  GET  /quote?sku=FLU-TRAY-100&qty=1
       → 200 {sku, qty, unit_price, total, invoice_id, pay_to, chain, token}

  POST /settle
       body: {invoice_id, tx_hash}
       → 200 {ok: true, status: "PAID", ships_by: "..."}  (after on-chain verify)
       → 200 {ok: false, error: "..."}                    (tx not found / wrong recipient / wrong amount)

In production PharmaSupply would expose x402 as its payment interface; for the
hackathon demo we describe payment via an explicit invoice and on-chain settle
step. The settle step performs a real GOAT mainnet RPC check against the
USDC contract's Transfer event, so payment is genuinely verified.

Designed to be run on port 8645:
    python3 pharmasupply_server.py
"""

import json
import os
from collections import defaultdict, deque
import threading
import time
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8645

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(HERE, "pharmasupply_catalog.json")
INVOICES_PATH = os.path.join(HERE, "pharmasupply_invoices.json")
WALLET_PATH = os.path.join(HERE, ".pharmasupply-wallet.json")

# GOAT mainnet (chain 2345)
GOAT_RPC = "https://rpc.goat.network"
USDC_CONTRACT = "0x3022b87ac063DE95b1570F46f5e470F8B53112D8"
USDC_DECIMALS = 6
CHAIN_ID = 2345
MAX_BODY_BYTES = 64 * 1024
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 240
INVOICES_LOCK = threading.RLock()
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_BUCKETS: dict[str, deque[float]] = defaultdict(deque)


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
# keccak256("Transfer(address,address,uint256)")
ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


def load_address() -> str:
    with open(WALLET_PATH) as f:
        return json.load(f)["address"]


def load_catalog() -> dict:
    if os.path.exists(CATALOG_PATH):
        with open(CATALOG_PATH) as f:
            return json.load(f)
    # Default supplier catalog — independent from clinic-side inventory.
    return {
        "FLU-TRAY-100": {
            "name": "Fluoride trays (box of 100)",
            "keywords": ["fluoride", "trays", "tray", "flu"],
            "unit_price_usd": 0.50,
            "lead_time_days": 2,
        },
        "GLV-NIT-200": {
            "name": "Nitrile gloves (box of 200)",
            "keywords": ["nitrile", "gloves", "glove", "glv"],
            "unit_price_usd": 1.20,
            "lead_time_days": 2,
        },
        "MASK-50": {
            "name": "Surgical masks (box of 50)",
            "keywords": ["surgical", "mask", "masks", "ppe"],
            "unit_price_usd": 0.80,
            "lead_time_days": 2,
        },
        "IBU-100MG-100": {
            "name": "Ibuprofen 100mg (bottle of 100)",
            "keywords": ["ibuprofen", "ibu", "advil", "painkiller", "nsaid"],
            "unit_price_usd": 0.25,
            "lead_time_days": 3,
        },
        "GAUZE-4X4-200": {
            "name": "Sterile gauze 4x4 (pack of 200)",
            "keywords": ["gauze", "sterile", "dressing", "pad"],
            "unit_price_usd": 0.40,
            "lead_time_days": 2,
        },
    }


def _tokenize(s: str) -> list[str]:
    return [t for t in (s or "").lower().replace("-", " ").replace("_", " ").split() if t]


def _token_matches(query_token: str, haystack_tokens: set) -> bool:
    """Match if any haystack token starts with the query token, or vice
    versa, OR they share a 4+ char common prefix (handles 'ibuprofen' ↔ 'ibu').
    Pure prefix avoids the 'every-token-contains-a-vowel' false positives.
    """
    qt = query_token.lower()
    if len(qt) < 3:
        # Too short to be informative; require exact-token match
        return qt in haystack_tokens
    for h in haystack_tokens:
        if h == qt or h.startswith(qt) or qt.startswith(h):
            return True
        # Share at least the first 4 chars
        if len(h) >= 4 and len(qt) >= 4 and h[:4] == qt[:4]:
            return True
    return False


def search_catalog(query: str) -> list[dict]:
    """Score SKUs against a free-text query. Returns matches sorted by score desc.

    For each query token, +1 if it matches the SKU's haystack (name + keywords +
    SKU string). Items with zero matches are filtered out. Ties broken by SKU asc.
    """
    catalog = load_catalog()
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    matches = []
    for sku, item in catalog.items():
        haystack = " ".join([
            sku.lower(),
            (item.get("name") or "").lower(),
            " ".join(item.get("keywords") or []),
        ])
        haystack_tokens = set(_tokenize(haystack))
        score = sum(1 for t in q_tokens if _token_matches(t, haystack_tokens))
        if score > 0:
            matches.append((score, sku, item))
    matches.sort(key=lambda x: (-x[0], x[1]))
    return [
        {
            "sku": sku,
            "name": item.get("name"),
            "unit_price_usd": float(item.get("unit_price_usd", 0)),
            "lead_time_days": int(item.get("lead_time_days", 0)),
            "score": score,
        }
        for score, sku, item in matches
    ]


def save_invoices(d: dict) -> None:
    with INVOICES_LOCK:
        tmp = INVOICES_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, INVOICES_PATH)


def load_invoices() -> dict:
    with INVOICES_LOCK:
        if not os.path.exists(INVOICES_PATH):
            return {}
        try:
            with open(INVOICES_PATH) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            print(f"[pharmasupply] invoice read error: {e}", flush=True)
            return {}


def rpc_call(method: str, params: list) -> dict:
    """Make a single JSON-RPC call to the GOAT mainnet RPC."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        GOAT_RPC,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def addr_to_topic(addr: str) -> str:
    """Pad a 0x-prefixed 20-byte address into a 32-byte topic value."""
    a = addr.lower().replace("0x", "")
    return "0x" + ("0" * 24) + a


def verify_usdc_transfer(tx_hash: str, expected_to: str, expected_amount_usd: float) -> tuple:
    """Verify on-chain that tx_hash transferred at least expected_amount_usd USDC to expected_to.

    Returns (ok, info) where info is either an error message or a details dict.
    """
    try:
        receipt = rpc_call("eth_getTransactionReceipt", [tx_hash]).get("result")
    except Exception as e:
        return False, f"RPC unreachable ({e})"
    if not receipt:
        return False, "Transaction not found on chain yet — wait a few seconds and retry"
    if str(receipt.get("status")).lower() not in ("0x1", "1"):
        return False, "Transaction failed on chain"

    expected_topic = addr_to_topic(expected_to)
    needed_raw = int(round(expected_amount_usd * (10 ** USDC_DECIMALS)))

    for lg in receipt.get("logs", []):
        if (lg.get("address") or "").lower() != USDC_CONTRACT.lower():
            continue
        topics = lg.get("topics") or []
        if len(topics) < 3:
            continue
        if topics[0].lower() != ERC20_TRANSFER_TOPIC:
            continue
        if topics[2].lower() != expected_topic.lower():
            continue
        try:
            amount_raw = int(lg.get("data", "0x0"), 16)
        except Exception:
            continue
        if amount_raw < needed_raw:
            continue
        return True, {
            "from": "0x" + topics[1][-40:],
            "to": expected_to,
            "amount_raw": amount_raw,
            "amount_usd": amount_raw / (10 ** USDC_DECIMALS),
            "block_number": receipt.get("blockNumber"),
        }
    return False, (
        f"No USDC Transfer of >= ${expected_amount_usd:.2f} to {expected_to} "
        f"found in the transaction's logs"
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[pharmasupply] {self.address_string()} - {fmt % args}", flush=True)

    def _json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rate_limited(self) -> bool:
        path = urlparse(self.path).path
        if path in ("/health", "/v1/health"):
            return False
        key = self.client_address[0]
        now = time.monotonic()
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        with RATE_LIMIT_LOCK:
            bucket = RATE_LIMIT_BUCKETS[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
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
            print(f"[pharmasupply] unhandled GET error: {e}", flush=True)
            self._safe_json_error(500, "internal error")

    def _do_GET(self):
        if self._rate_limited():
            return self._json(429, {"ok": False, "error": "rate_limited"})
        url = urlparse(self.path)
        if url.path in ("/health", "/v1/health"):
            return self._json(200, {"status": "ok", "service": "pharmasupply"})

        if url.path == "/quote":
            qs = parse_qs(url.query)
            sku = (qs.get("sku") or [""])[0].strip()
            try:
                qty = max(1, int((qs.get("qty") or ["1"])[0]))
            except ValueError:
                qty = 1

            catalog = load_catalog()
            item = catalog.get(sku)
            if not item:
                return self._json(404, {"ok": False, "error": "unknown_sku", "sku": sku})

            unit = float(item["unit_price_usd"])
            total = round(unit * qty, 2)
            invoice_id = f"INV-{uuid.uuid4().hex[:8].upper()}"
            ships_by_unix = int(time.time()) + int(item["lead_time_days"]) * 86400
            ships_by = time.strftime("%Y-%m-%d", time.localtime(ships_by_unix))

            with INVOICES_LOCK:
                invoices = load_invoices()
                invoices[invoice_id] = {
                    "invoice_id": invoice_id,
                    "sku": sku,
                    "name": item["name"],
                    "qty": qty,
                    "unit_price_usd": unit,
                    "total_usd": total,
                    "pay_to": load_address(),
                    "chain_id": CHAIN_ID,
                    "token": USDC_CONTRACT,
                    "ships_by": ships_by,
                    "status": "AWAITING_PAYMENT",
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_invoices(invoices)
                invoice = invoices[invoice_id]

            return self._json(200, invoice)

        if url.path == "/search":
            qs = parse_qs(url.query)
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                return self._json(400, {"ok": False, "error": "missing_query"})
            return self._json(200, {"ok": True, "query": q, "matches": search_catalog(q)})

        if url.path == "/catalog":
            return self._json(200, {"ok": True, "catalog": load_catalog()})

        if url.path == "/invoices":
            return self._json(200, {"invoices": load_invoices()})

        self.send_error(404)

    def do_POST(self):
        try:
            self._do_POST()
        except RequestError as e:
            self._safe_json_error(e.status, e.message)
        except Exception as e:
            print(f"[pharmasupply] unhandled POST error: {e}", flush=True)
            self._safe_json_error(500, "internal error")

    def _do_POST(self):
        if self._rate_limited():
            return self._json(429, {"ok": False, "error": "rate_limited"})
        if self.path != "/settle":
            self.send_error(404)
            return

        payload = self._read_json_body()

        invoice_id = (payload.get("invoice_id") or "").strip()
        tx_hash = (payload.get("tx_hash") or "").strip()
        if not invoice_id or not tx_hash:
            return self._json(
                200,
                {
                    "ok": False,
                    "error": "missing_fields",
                    "message": "invoice_id and tx_hash are both required",
                },
            )

        with INVOICES_LOCK:
            invoices = load_invoices()
            inv = invoices.get(invoice_id)
            if inv is None:
                return self._json(200, {"ok": False, "error": "unknown_invoice"})

            if inv["status"] == "PAID":
                return self._json(
                    200,
                    {
                        "ok": True,
                        "status": "PAID",
                        "ships_by": inv["ships_by"],
                        "message": "Invoice was already settled",
                        "tx_hash": inv.get("tx_hash"),
                    },
                )

        ok, info = verify_usdc_transfer(
            tx_hash=tx_hash,
            expected_to=inv["pay_to"],
            expected_amount_usd=float(inv["total_usd"]),
        )
        if not ok:
            return self._json(
                200,
                {
                    "ok": False,
                    "error": "verification_failed",
                    "tx_hash": tx_hash,
                    "reason": info,
                },
            )

        with INVOICES_LOCK:
            invoices = load_invoices()
            inv = invoices.get(invoice_id, inv)
            inv["status"] = "PAID"
            inv["tx_hash"] = tx_hash
            inv["paid_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            inv["payment_info"] = info
            invoices[invoice_id] = inv
            save_invoices(invoices)

        print(
            f"[pharmasupply] settled {invoice_id} via {tx_hash} "
            f"(${info['amount_usd']:.2f} from {info['from']})",
            flush=True,
        )
        return self._json(
            200,
            {
                "ok": True,
                "status": "PAID",
                "invoice_id": invoice_id,
                "tx_hash": tx_hash,
                "ships_by": inv["ships_by"],
                "amount_usd": info["amount_usd"],
                "block_number": info["block_number"],
                "explorer_url": f"https://explorer.goat.network/tx/{tx_hash}",
            },
        )


def main():
    addr = load_address()
    print(f"PharmaSupply receiving wallet: {addr}", flush=True)
    print(f"Listening on http://127.0.0.1:{PORT}", flush=True)
    print(f"  GET  /quote?sku=FLU-TRAY-100&qty=1", flush=True)
    print(f"  POST /settle  body={{invoice_id, tx_hash}}", flush=True)
    print(f"  GET  /invoices", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
