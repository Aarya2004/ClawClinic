"""ClawClinic procurement client — autonomous A2A restock.

Used by the /restock slash command. Steps:

  1. Read inventory.json, find low-stock items.
  2. For each low-stock item, ask PharmaSupply for a quote.
  3. If total spend <= AUTONOMOUS_LIMIT_USD, broadcast a real USDC transfer
     on GOAT mainnet via the evm-wallet-skill.
     If over the limit, return a confirmation token that the user must
     literally repeat back to proceed.
  4. POST {invoice_id, tx_hash} to PharmaSupply /settle. PharmaSupply
     verifies the transfer on-chain before marking the invoice PAID.
  5. Update inventory.json (on_hand += qty) on success.

Usage (CLI for the demo / Hermes plugin):

  python3 procurement_client.py restock                 # autonomous up to limit
  python3 procurement_client.py restock --confirm-token CONFIRM-RESTOCK-XYZ

  python3 procurement_client.py inventory               # show current inventory
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
INVENTORY_PATH = os.path.join(HERE, "inventory.json")
WALLET_SKILL_DIR = os.path.abspath(
    os.path.join(HERE, "..", ".claude", "skills", "evm-wallet-skill")
)
PENDING_PATH = os.path.join(HERE, ".pending_restocks.json")

PHARMASUPPLY_URL = os.environ.get("PHARMASUPPLY_URL", "http://127.0.0.1:8645")
GOAT_CHAIN_KEY = "goat"
USDC_CONTRACT = "0x3022b87ac063DE95b1570F46f5e470F8B53112D8"

# Spending limits live in clinic_config.json so /onboard can change them at
# runtime. Falls back to these defaults only if the config module fails to
# import (e.g. running the client in isolation).
DEFAULT_AUTONOMOUS_LIMIT_USD = 5.00
DEFAULT_DAILY_CAP_USD = 50.00

try:
    import clinic_config as _clinic_config
except Exception:  # noqa: BLE001
    _clinic_config = None


def autonomous_limit_usd() -> float:
    if _clinic_config is not None:
        try:
            return float(_clinic_config.autonomous_limit_usd())
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_AUTONOMOUS_LIMIT_USD


def daily_cap_usd() -> float:
    if _clinic_config is not None:
        try:
            return float(_clinic_config.daily_cap_usd())
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_DAILY_CAP_USD


def spend_last_24h_usd() -> float:
    if _clinic_config is not None:
        try:
            return float(_clinic_config.spend_last_24h())
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def record_spend(amount_usd: float, tx_hash: str, sku: str) -> None:
    if _clinic_config is not None:
        try:
            _clinic_config.record_spend(amount_usd, tx_hash, sku)
        except Exception:  # noqa: BLE001
            pass


# ---------- Inventory ----------

def load_inventory() -> dict:
    with open(INVENTORY_PATH) as f:
        return json.load(f)


def save_inventory(inv: dict) -> None:
    with open(INVENTORY_PATH, "w") as f:
        json.dump(inv, f, indent=2)


def low_stock_items(inv: dict) -> list:
    out = []
    for sku, row in inv.items():
        if int(row.get("on_hand", 0)) < int(row.get("threshold", 0)):
            out.append((sku, row))
    return out


# ---------- PharmaSupply ----------

def http_get(url: str, timeout: int = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def http_post(url: str, body: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_quote(sku: str, qty: int = 1) -> dict:
    return http_get(f"{PHARMASUPPLY_URL}/quote?sku={sku}&qty={qty}")


def settle_invoice(invoice_id: str, tx_hash: str) -> dict:
    return http_post(
        f"{PHARMASUPPLY_URL}/settle",
        {"invoice_id": invoice_id, "tx_hash": tx_hash},
    )


# ---------- Pending-confirmation persistence ----------

def load_pending() -> dict:
    if not os.path.exists(PENDING_PATH):
        return {}
    with open(PENDING_PATH) as f:
        return json.load(f)


def save_pending(d: dict) -> None:
    with open(PENDING_PATH, "w") as f:
        json.dump(d, f, indent=2)


# ---------- Wallet: real on-chain USDC transfer ----------

def send_usdc(to_addr: str, amount_usd: float) -> dict:
    """Shell out to the evm-wallet-skill to broadcast a real USDC transfer
    on GOAT mainnet. Returns the skill's JSON result.
    """
    if not os.path.isdir(WALLET_SKILL_DIR):
        return {"success": False, "error": f"wallet skill not found at {WALLET_SKILL_DIR}"}
    cmd = [
        "node",
        "src/transfer.js",
        GOAT_CHAIN_KEY,
        to_addr,
        f"{amount_usd:.6f}",
        USDC_CONTRACT,
        "--yes",
        "--json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=WALLET_SKILL_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "transfer command timed out after 180s"}
    if proc.returncode != 0:
        # Try to extract a JSON error from stdout; fall back to stderr.
        try:
            return json.loads(proc.stdout)
        except Exception:
            return {
                "success": False,
                "error": "transfer failed",
                "stderr": (proc.stderr or "")[-400:],
                "stdout": (proc.stdout or "")[-400:],
            }
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {
            "success": False,
            "error": "could not parse transfer output",
            "stdout": (proc.stdout or "")[-400:],
        }


# ---------- Demo formatting ----------

def fmt_section(title: str) -> str:
    return f"\n{title}\n" + ("-" * len(title))


def fmt_restock_report(report: dict) -> str:
    lines = ["Inventory check:"]
    for sku, row in report.get("inventory", {}).items():
        marker = "⚠️  LOW" if int(row["on_hand"]) < int(row["threshold"]) else "ok"
        lines.append(f"  {row['name']}: {row['on_hand']} remaining (threshold {row['threshold']}) — {marker}")

    for step in report.get("steps", []):
        lines.append("")
        lines.append(step)

    if report.get("ok"):
        lines.append("")
        lines.append("✅ Restock complete.")
    elif report.get("requires_confirmation"):
        lines.append("")
        lines.append(
            f"⚠️  Total spend ${report['total_usd']:.2f} exceeds the {report.get('limit_kind','autonomous')} limit "
            f"${report.get('limit_usd', 0):.2f}."
        )
        lines.append(
            f"Reply with this exact token to proceed:  {report['confirmation_token']}"
        )
    else:
        lines.append("")
        lines.append(f"❌ Restock did not complete: {report.get('error','unknown error')}")
    return "\n".join(lines)


# ---------- Main flows ----------

def cmd_inventory() -> int:
    inv = load_inventory()
    print(json.dumps(inv, indent=2))
    return 0


def cmd_restock(confirm_token: str | None = None) -> int:
    inv = load_inventory()
    needed = low_stock_items(inv)
    report = {"inventory": inv, "steps": [], "ok": False}

    if not needed:
        report["steps"].append("All items above threshold. Nothing to restock.")
        report["ok"] = True
        print(fmt_restock_report(report))
        return 0

    # Build the planned orders + total
    plans = []
    total = 0.0
    for sku, row in needed:
        qty = max(1, int(row.get("threshold", 0)) - int(row.get("on_hand", 0)))
        # Cap quantity at 1 for the demo: one box covers the demo threshold reset.
        qty = 1
        unit = float(row.get("unit_price_usd", 0))
        plans.append({"sku": sku, "qty": qty, "expected_unit_usd": unit, "row": row})
        total += unit * qty

    report["total_usd"] = total
    pending = load_pending()

    # Snapshot guardrail limits at call time so the report shows what was in
    # effect for this run.
    limit_per_run = autonomous_limit_usd()
    limit_daily = daily_cap_usd()
    spent_24h = spend_last_24h_usd()
    report["spent_last_24h_usd"] = spent_24h
    report["autonomous_limit_usd"] = limit_per_run
    report["daily_cap_usd"] = limit_daily

    # Two stacked guardrails: per-restock and rolling 24h.
    over_per_run = total > limit_per_run
    over_daily = (spent_24h + total) > limit_daily

    if over_per_run or over_daily:
        limit_kind = "per-restock" if over_per_run else "rolling 24h"
        limit_usd = limit_per_run if over_per_run else limit_daily
        if not confirm_token:
            token = f"CONFIRM-RESTOCK-{uuid.uuid4().hex[:6].upper()}"
            pending[token] = {
                "plans": [{k: v for k, v in p.items() if k != "row"} for p in plans],
                "total_usd": total,
                "limit_kind": limit_kind,
                "limit_usd": limit_usd,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_pending(pending)
            report["requires_confirmation"] = True
            report["confirmation_token"] = token
            report["limit_kind"] = limit_kind
            report["limit_usd"] = limit_usd
            if over_per_run:
                report["steps"].append(
                    f"Planned spend ${total:.2f} exceeds per-restock limit "
                    f"(${limit_per_run:.2f}). Operator confirmation required."
                )
            else:
                report["steps"].append(
                    f"Planned spend ${total:.2f} on top of last 24h ${spent_24h:.2f} "
                    f"would exceed daily cap (${limit_daily:.2f}). Operator confirmation required."
                )
            print(fmt_restock_report(report))
            return 2  # not an error, just pending confirmation
        if confirm_token not in pending:
            report["error"] = "unknown confirmation token"
            print(fmt_restock_report(report))
            return 1
        # Token consumed
        del pending[confirm_token]
        save_pending(pending)
        report["steps"].append(
            f"Operator confirmation token accepted. Proceeding with ${total:.2f} spend."
        )
    else:
        report["steps"].append(
            f"Total spend ${total:.2f} ≤ per-restock ${limit_per_run:.2f} and "
            f"24h cumulative ${spent_24h:.2f} + ${total:.2f} ≤ daily ${limit_daily:.2f}. "
            "Proceeding autonomously."
        )

    # Execute each plan: quote -> pay -> settle -> update inventory
    for plan in plans:
        sku = plan["sku"]
        qty = plan["qty"]
        try:
            quote = fetch_quote(sku, qty)
        except urllib.error.URLError as e:
            report["error"] = f"PharmaSupply unreachable ({e})"
            print(fmt_restock_report(report))
            return 1
        if "error" in quote:
            report["error"] = f"quote failed for {sku}: {quote.get('error')}"
            print(fmt_restock_report(report))
            return 1

        report["steps"].append(
            f"PharmaSupply quote {quote['invoice_id']}: {qty}× {quote['name']} "
            f"@ ${quote['unit_price_usd']:.2f} = ${quote['total_usd']:.2f} "
            f"(ships by {quote['ships_by']})"
        )

        # Broadcast the USDC transfer
        report["steps"].append(
            f"Sending {quote['total_usd']:.2f} USDC to {quote['pay_to'][:10]}…{quote['pay_to'][-4:]} on GOAT mainnet…"
        )
        tx_result = send_usdc(quote["pay_to"], float(quote["total_usd"]))
        if not tx_result.get("success") and not (
            tx_result.get("txHash") or tx_result.get("hash")
        ):
            report["error"] = (
                "USDC transfer failed: "
                + (tx_result.get("error") or json.dumps(tx_result))[:300]
            )
            print(fmt_restock_report(report))
            return 1
        tx_hash = (
            tx_result.get("txHash")
            or tx_result.get("hash")
            or tx_result.get("transactionHash")
        )
        if not tx_hash:
            report["error"] = "transfer succeeded but no tx hash returned"
            print(fmt_restock_report(report))
            return 1

        report["steps"].append(
            f"Tx broadcast: {tx_hash}\nExplorer: https://explorer.goat.network/tx/{tx_hash}"
        )

        # Settle with PharmaSupply — they verify on-chain
        # Small delay so the receipt has time to land on the RPC
        time.sleep(3)
        attempts = 0
        last_settle = None
        while attempts < 5:
            try:
                last_settle = settle_invoice(quote["invoice_id"], tx_hash)
            except urllib.error.URLError as e:
                report["error"] = f"settle call failed: {e}"
                print(fmt_restock_report(report))
                return 1
            if last_settle.get("ok"):
                break
            attempts += 1
            time.sleep(3)

        if not last_settle or not last_settle.get("ok"):
            report["error"] = (
                f"PharmaSupply could not verify tx after {attempts} attempts: "
                + str((last_settle or {}).get("reason", "unknown"))
            )
            report["steps"].append(
                "(The transfer DID broadcast — verification just timed out. "
                "Try again in a minute.)"
            )
            print(fmt_restock_report(report))
            return 1

        report["steps"].append(
            f"PharmaSupply confirmed payment: ${last_settle['amount_usd']:.2f} "
            f"settled in block {last_settle['block_number']}. Ships by {last_settle['ships_by']}."
        )

        # Record into the rolling 24h spend ledger for the next guardrail check
        record_spend(float(last_settle["amount_usd"]), tx_hash, sku)

        # Update inventory
        inv[sku]["on_hand"] = int(inv[sku]["on_hand"]) + qty * 100
        # (Each box of fluoride trays contains 100 units in the demo SKU.)
        save_inventory(inv)
        report["steps"].append(
            f"Inventory updated: {inv[sku]['name']} now {inv[sku]['on_hand']} remaining."
        )

    report["ok"] = True
    print(fmt_restock_report(report))
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="procurement_client")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory")
    restock = sub.add_parser("restock")
    restock.add_argument("--confirm-token", default=None)
    args = p.parse_args(argv[1:])

    if args.cmd == "inventory":
        return cmd_inventory()
    if args.cmd == "restock":
        return cmd_restock(args.confirm_token)
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
