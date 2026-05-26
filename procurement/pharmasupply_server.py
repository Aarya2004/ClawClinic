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
            "unit_price_usd": 0.50,
            "lead_time_days": 2,
        },
        "GLV-NIT-200": {
            "name": "Nitrile gloves (200 ct)",
            "unit_price_usd": 1.20,
            "lead_time_days": 2,
        },
        "MASK-50": {
            "name": "Surgical masks (50 ct)",
            "unit_price_usd": 0.80,
            "lead_time_days": 2,
        },
    }


def save_invoices(d: dict) -> None:
    with open(INVOICES_PATH, "w") as f:
        json.dump(d, f, indent=2)


def load_invoices() -> dict:
    if not os.path.exists(INVOICES_PATH):
        return {}
    with open(INVOICES_PATH) as f:
        return json.load(f)


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

    def do_GET(self):
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

            return self._json(200, invoices[invoice_id])

        if url.path == "/invoices":
            return self._json(200, {"invoices": load_invoices()})

        self.send_error(404)

    def do_POST(self):
        if self.path != "/settle":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json(400, {"ok": False, "error": "bad_json"})

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
