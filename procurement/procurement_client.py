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
    try:
        with open(INVENTORY_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_inventory(inv: dict) -> None:
    tmp = INVENTORY_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(inv, f, indent=2)
    os.replace(tmp, INVENTORY_PATH)


def low_stock_items(inv: dict) -> list:
    out = []
    for sku, row in inv.items():
        try:
            on_hand = int(row.get("on_hand", 0))
            threshold = int(row.get("threshold", 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if on_hand < threshold:
            out.append((sku, row))
    return out


# ---------- PharmaSupply ----------

def http_get(url: str, timeout: int = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = json.loads(r.read())
        return data if isinstance(data, dict) else {"error": "bad_response"}


def http_post(url: str, body: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
        return data if isinstance(data, dict) else {"error": "bad_response"}


def fetch_quote(sku: str, qty: int = 1) -> dict:
    return http_get(f"{PHARMASUPPLY_URL}/quote?sku={sku}&qty={qty}")


def search_pharmasupply(query: str) -> list:
    """Fuzzy-search PharmaSupply's catalog by free-text item name."""
    import urllib.parse as _u
    resp = http_get(f"{PHARMASUPPLY_URL}/search?q={_u.quote(query)}")
    return resp.get("matches") or []


def settle_invoice(invoice_id: str, tx_hash: str) -> dict:
    return http_post(
        f"{PHARMASUPPLY_URL}/settle",
        {"invoice_id": invoice_id, "tx_hash": tx_hash},
    )


# ---------- Pending-confirmation persistence ----------

def load_pending() -> dict:
    if not os.path.exists(PENDING_PATH):
        return {}
    try:
        with open(PENDING_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_pending(d: dict) -> None:
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, PENDING_PATH)


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
        try:
            on_hand = int(row.get("on_hand", 0))
            threshold = int(row.get("threshold", 0))
        except (AttributeError, TypeError, ValueError):
            lines.append(f"  {sku}: invalid inventory row")
            continue
        marker = "⚠️  LOW" if on_hand < threshold else "ok"
        lines.append(f"  {row.get('name', sku)}: {on_hand} remaining (threshold {threshold}) — {marker}")

    for step in report.get("steps", []):
        lines.append("")
        lines.append(step)

    if report.get("ok"):
        lines.append("")
        lines.append("✅ Restock complete.")
    elif report.get("requires_confirmation"):
        lines.append("")
        lines.append(
            f"Reply with this exact token to confirm and execute:  "
            f"{report['confirmation_token']}"
        )
        lines.append(
            "  (Send as:  /restock " + report['confirmation_token'] + ")"
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


def _build_plans_from_inventory(inv: dict) -> tuple[list, str | None]:
    """Auto-mode: order 1 unit of each low-stock SKU."""
    plans = []
    for sku, row in low_stock_items(inv):
        try:
            unit = float(row.get("unit_price_usd", 0))
        except (TypeError, ValueError):
            return [], f"invalid unit_price_usd for {sku}"
        plans.append({"sku": sku, "qty": 1, "expected_unit_usd": unit})
    return plans, None


def _resolve_explicit(sku: str | None, query: str | None, qty: int) -> tuple[list, str | None]:
    """Explicit mode: resolve a single SKU+qty (with optional fuzzy query)."""
    if qty <= 0:
        return [], "Quantity must be a positive integer."

    if sku:
        try:
            quote = fetch_quote(sku, qty)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            return [], f"PharmaSupply unreachable ({e})"
        if "error" in quote:
            return [], f"SKU {sku!r} not in PharmaSupply's catalog."
        return [{
            "sku": sku,
            "qty": qty,
            "expected_unit_usd": float(quote.get("unit_price_usd", 0)),
            "resolved_name": quote.get("name"),
        }], None

    if query:
        try:
            matches = search_pharmasupply(query)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            return [], f"PharmaSupply unreachable ({e})"
        if not matches:
            return [], f"PharmaSupply has no match for {query!r}."
        # Take the best match; show ambiguity in the report
        top = matches[0]
        return [{
            "sku": top["sku"],
            "qty": qty,
            "expected_unit_usd": float(top.get("unit_price_usd", 0)),
            "resolved_name": top.get("name"),
            "search_query": query,
            "alternatives": [m["sku"] for m in matches[1:3]],
        }], None

    return [], "Either --sku or --query is required for explicit restock."


def cmd_restock(
    confirm_token: str | None = None,
    sku: str | None = None,
    qty: int = 1,
    query: str | None = None,
) -> int:
    inv = load_inventory()
    report: dict = {"inventory": inv, "steps": [], "ok": False}

    # 1) If the user supplied a confirm-token, look it up and apply that plan.
    pending = load_pending()
    if confirm_token:
        prop = pending.get(confirm_token)
        if not prop:
            report["error"] = (
                f"Unknown confirmation token {confirm_token}. "
                "Tokens are single-use and only valid in the session that proposed them."
            )
            print(fmt_restock_report(report))
            return 1
        plans = prop.get("plans") or []
        total = float(prop.get("total_usd", 0))
        report["steps"].append(
            f"Operator confirmation token {confirm_token} accepted. "
            f"Executing the staged ${total:.2f} order."
        )
        # Re-check daily cap at apply time (24h window may have rolled or other
        # spends may have happened between propose and confirm).
        recheck = spend_last_24h_usd() + total
        if recheck > daily_cap_usd():
            del pending[confirm_token]
            save_pending(pending)
            report["error"] = (
                f"Cumulative 24h spend ${recheck:.2f} would exceed daily cap "
                f"${daily_cap_usd():.2f}. Confirmation void; please re-propose."
            )
            print(fmt_restock_report(report))
            return 1
        del pending[confirm_token]
        save_pending(pending)
        report["total_usd"] = total
        report["autonomous_limit_usd"] = autonomous_limit_usd()
        report["daily_cap_usd"] = daily_cap_usd()
        report["spent_last_24h_usd"] = spend_last_24h_usd()
        return _execute_plans(plans, report, inv)

    # 2) Build the plans. Explicit path takes precedence over auto-detect.
    if sku or query:
        plans, err = _resolve_explicit(sku, query, qty)
        mode = "explicit"
    else:
        plans, err = _build_plans_from_inventory(inv)
        mode = "auto"

    if err:
        report["error"] = err
        print(fmt_restock_report(report))
        return 1

    if not plans:
        report["steps"].append("All items above threshold. Nothing to restock.")
        report["ok"] = True
        print(fmt_restock_report(report))
        return 0

    # 3) Fetch live quotes so the propose total is what the user will actually
    # be charged.
    total = 0.0
    quoted_plans = []
    for plan in plans:
        try:
            quote = fetch_quote(plan["sku"], plan["qty"])
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            report["error"] = f"PharmaSupply unreachable while quoting ({e})"
            print(fmt_restock_report(report))
            return 1
        if "error" in quote:
            report["error"] = f"quote failed for {plan['sku']}: {quote.get('error')}"
            print(fmt_restock_report(report))
            return 1
        quoted_plans.append({
            **plan,
            "name": quote.get("name", plan["sku"]),
            "quoted_unit_usd": float(quote.get("unit_price_usd", 0)),
            "quoted_total_usd": float(quote.get("total_usd", 0)),
            "ships_by": quote.get("ships_by"),
        })
        total += float(quote.get("total_usd", 0))

    # 4) ALWAYS stage a CONFIRM-RESTOCK token. No silent autonomous spending.
    limit_per_run = autonomous_limit_usd()
    limit_daily = daily_cap_usd()
    spent_24h = spend_last_24h_usd()
    over_per_run = total > limit_per_run
    over_daily = (spent_24h + total) > limit_daily

    if mode == "auto":
        report["steps"].append("Inventory check found items below threshold:")
        for p in quoted_plans:
            report["steps"].append(
                f"  • {p['name']} — propose {p['qty']}× @ ${p['quoted_unit_usd']:.2f} "
                f"= ${p['quoted_total_usd']:.2f}"
            )
    else:
        for p in quoted_plans:
            line = (
                f"PharmaSupply quoted: {p['qty']}× {p['name']} "
                f"@ ${p['quoted_unit_usd']:.2f} = ${p['quoted_total_usd']:.2f}"
            )
            if p.get("alternatives"):
                line += f"  (also matched: {', '.join(p['alternatives'])})"
            report["steps"].append(line)

    report["total_usd"] = total
    report["spent_last_24h_usd"] = spent_24h
    report["autonomous_limit_usd"] = limit_per_run
    report["daily_cap_usd"] = limit_daily

    token = f"CONFIRM-RESTOCK-{uuid.uuid4().hex[:6].upper()}"
    pending[token] = {
        "plans": [{k: v for k, v in p.items() if k not in ("alternatives", "search_query")} for p in quoted_plans],
        "total_usd": total,
        "limit_kind": "per-restock" if over_per_run else ("rolling 24h" if over_daily else "below all limits"),
        "limit_usd": limit_per_run if over_per_run else (limit_daily if over_daily else 0),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
    }
    save_pending(pending)
    report["requires_confirmation"] = True
    report["confirmation_token"] = token

    if over_per_run:
        report["steps"].append(
            f"⚠️  ${total:.2f} exceeds per-restock limit ${limit_per_run:.2f}. "
            "Operator confirmation required (high-risk)."
        )
    elif over_daily:
        report["steps"].append(
            f"⚠️  ${total:.2f} on top of last 24h ${spent_24h:.2f} would exceed "
            f"daily cap ${limit_daily:.2f}. Operator confirmation required (high-risk)."
        )
    else:
        report["steps"].append(
            f"${total:.2f} is within all limits (per-restock ${limit_per_run:.2f}, "
            f"daily ${limit_daily:.2f}). Operator confirmation still required before any spend."
        )

    print(fmt_restock_report(report))
    return 2  # pending confirmation


def _execute_plans(plans: list, report: dict, inv: dict) -> int:

    # Execute each plan: quote -> pay -> settle -> update inventory
    for plan in plans:
        sku = plan["sku"]
        qty = plan["qty"]
        try:
            quote = fetch_quote(sku, qty)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            report["error"] = f"PharmaSupply unreachable ({e})"
            print(fmt_restock_report(report))
            return 1
        if "error" in quote:
            report["error"] = f"quote failed for {sku}: {quote.get('error')}"
            print(fmt_restock_report(report))
            return 1
        required_quote_fields = {
            "invoice_id", "name", "unit_price_usd", "total_usd",
            "ships_by", "pay_to",
        }
        if not required_quote_fields.issubset(quote):
            missing = ", ".join(sorted(required_quote_fields - set(quote)))
            report["error"] = f"quote response missing fields: {missing}"
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
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
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

        # Update inventory. If the SKU isn't tracked yet (the user just ordered
        # something new via /restock <name> <qty>), seed a row with sensible
        # defaults so future auto-detect can reason about it.
        if sku not in inv:
            inv[sku] = {
                "name": quote.get("name", sku),
                "on_hand": 0,
                "threshold": 0,
                "unit_price_usd": float(quote.get("unit_price_usd", 0)),
                "supplier_sku": sku,
            }
        try:
            current = int(inv[sku].get("on_hand", 0))
        except (TypeError, ValueError):
            current = 0
        # qty here is the number of supplier units (e.g. boxes). For the demo
        # we track the same unit on the clinic side: 1 box ordered = 1 unit
        # added to on_hand. (Previous behaviour multiplied by 100 because the
        # legacy fluoride row counted individual trays, which broke for any
        # non-tray SKU.)
        inv[sku]["on_hand"] = current + qty
        save_inventory(inv)
        report["steps"].append(
            f"Inventory updated: {inv[sku].get('name', sku)} now {inv[sku]['on_hand']} on hand."
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
    restock.add_argument(
        "--sku",
        default=None,
        help="Explicit supplier SKU (skips fuzzy search).",
    )
    restock.add_argument(
        "--query",
        default=None,
        help='Free-text item name to fuzzy-match against PharmaSupply (e.g. "ibuprofen").',
    )
    restock.add_argument(
        "--qty",
        type=int,
        default=1,
        help="Quantity of supplier units to order (default 1). Required for explicit mode.",
    )

    search = sub.add_parser("search", help="Fuzzy-search PharmaSupply by item name.")
    search.add_argument("query", nargs="+")

    args = p.parse_args(argv[1:])

    if args.cmd == "inventory":
        return cmd_inventory()
    if args.cmd == "restock":
        return cmd_restock(
            confirm_token=args.confirm_token,
            sku=args.sku,
            qty=max(1, int(args.qty)),
            query=args.query,
        )
    if args.cmd == "search":
        q = " ".join(args.query)
        matches = search_pharmasupply(q)
        if not matches:
            print(f"No matches for {q!r}.")
            return 1
        print(f"PharmaSupply matches for {q!r}:")
        for m in matches:
            print(f"  [{m['score']}] {m['sku']}  {m['name']}  ${m['unit_price_usd']:.2f}/unit  ships in {m['lead_time_days']}d")
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:  # noqa: BLE001
        print(f"Restock did not complete: {e}")
        sys.exit(1)
