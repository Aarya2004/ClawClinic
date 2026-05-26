#!/usr/bin/env python3
"""
ClawClinic x402 helper — create orders and poll status on GOAT Mainnet.

Usage:
    python x402.py create --amount 1.00 --payer 0xPAYER --order-id booking_42
    python x402.py status --order-id <x402_order_id>

Reads X402_API_KEY, X402_API_SECRET, X402_API_URL from env (or ~/.hermes/.env).
Defaults to GOAT mainnet portal.
"""
from __future__ import annotations  # 3.9-compat for `dict | None` style hints

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path
from typing import Optional

DEFAULT_API_URL = "https://x402-api.goat.network"
USDC_DECIMALS = 6


def load_env():
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k.strip(), v)


def sign(payload: dict, secret: str) -> str:
    # Drop empties, sort keys, k=v&... — per skill spec
    items = sorted((k, str(v)) for k, v in payload.items() if v not in (None, ""))
    msg = "&".join(f"{k}={v}" for k, v in items)
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def http(method: str, url: str, headers: dict, body: Optional[dict] = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"error": "bad_json", "body": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        # 402 is x402's "Payment Required" — it carries the payment instructions, not an error.
        if e.code == 402:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"error": "bad_json", "http_status": e.code, "body": raw}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return {"error": "http_error", "http_status": e.code, "body": parsed}
    except urllib.error.URLError as e:
        return {"error": "network_error", "message": str(e.reason), "url": url}
    except TimeoutError:
        return {"error": "timeout", "message": "x402 request timed out", "url": url}


def env_or_error(*names: str) -> Optional[dict]:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        return {
            "error": "missing_env",
            "message": "Missing required x402 environment variables",
            "missing": missing,
        }
    return None


def is_evm_address(value: str) -> bool:
    return isinstance(value, str) and value.startswith("0x") and len(value) == 42


def create_order(amount_usdc: float, payer: str, dapp_order_id: str,
                 chain_id: int = 2345, token: str = "USDC"):
    err = env_or_error("X402_API_KEY", "X402_API_SECRET")
    if err:
        return err
    if amount_usdc <= 0:
        return {"error": "invalid_amount", "message": "Amount must be greater than 0 USDC"}
    if not is_evm_address(payer):
        return {"error": "invalid_payer", "message": "Payer must be a 42-character 0x EVM address"}

    api_key = os.environ["X402_API_KEY"]
    secret = os.environ["X402_API_SECRET"]
    base = os.environ.get("X402_API_URL", DEFAULT_API_URL)

    ts = str(int(time.time()))
    nonce = str(uuid.uuid4())
    amount_wei = str(int(amount_usdc * 10 ** USDC_DECIMALS))

    body = {
        "dapp_order_id": dapp_order_id,
        "chain_id": chain_id,
        "token_symbol": token,
        "from_address": payer,
        "amount_wei": amount_wei,
    }
    sig_payload = {**body, "api_key": api_key, "timestamp": ts, "nonce": nonce}
    sig = sign(sig_payload, secret)

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Sign": sig,
    }
    return http("POST", f"{base}/api/v1/orders", headers, body)


def get_status(order_id: str):
    err = env_or_error("X402_API_KEY", "X402_API_SECRET")
    if err:
        return err
    if not order_id.strip():
        return {"error": "invalid_order_id", "message": "Order ID is required"}

    api_key = os.environ["X402_API_KEY"]
    secret = os.environ["X402_API_SECRET"]
    base = os.environ.get("X402_API_URL", DEFAULT_API_URL)

    ts = str(int(time.time()))
    nonce = str(uuid.uuid4())
    sig = sign({"api_key": api_key, "timestamp": ts, "nonce": nonce}, secret)

    headers = {
        "X-API-Key": api_key,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Sign": sig,
    }
    return http("GET", f"{base}/api/v1/orders/{order_id}", headers)


def main():
    load_env()
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--amount", type=float, required=True, help="USDC amount, e.g. 1.00")
    c.add_argument("--payer", required=True, help="Payer wallet 0x...")
    c.add_argument("--order-id", required=True, help="Your internal order id")
    c.add_argument("--chain-id", type=int, default=2345)
    c.add_argument("--token", default="USDC")

    s = sub.add_parser("status")
    s.add_argument("--order-id", required=True, help="x402 order_id returned by create")

    args = p.parse_args()
    if args.cmd == "create":
        out = create_order(args.amount, args.payer, args.order_id, args.chain_id, args.token)
    else:
        out = get_status(args.order_id)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
