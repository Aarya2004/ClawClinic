"""ClawClinic plugin — slash commands for the Telegram bot.

Gateway dispatch sends each handler's return value DIRECTLY to the user
(see hermes-agent/gateway/run.py line 7666). So handlers must produce
finished user-facing replies — no LLM in the loop.

Static commands (/hours, /insurance, /identity) read JSON and reply.
Dynamic commands (/book, /cancel, /refill) reply with the first step
of a multi-turn flow; the user's next message is plain text, which the
LLM handles per SOUL.md + the clawclinic skill.
"""
from __future__ import annotations

import json
import os
import subprocess
import csv
from pathlib import Path


SKILL_DIR = Path.home() / ".hermes" / "skills" / "clawclinic"
SLOTS_FILE = SKILL_DIR / "slots.json"

# Procurement client lives in the ClawClinic repo. The plugin shells out to it
# for /restock so the agent-to-agent flow stays out-of-process and out of the
# Hermes turn-loop.
PROCUREMENT_DIR = Path(
    "/Users/aaryaprakash/Development/random_projs/GOAT-Hackathon-2026/procurement"
)
PROCUREMENT_CLIENT = PROCUREMENT_DIR / "procurement_client.py"
VOICE_BOOKINGS_CSV = Path(
    "/Users/aaryaprakash/Development/random_projs/GOAT-Hackathon-2026/voice/bookings.csv"
)

TESTNET_AGENT_ID = 304
MAINNET_AGENT_ID = 29
WALLET = "0x9deEC91428b2637c9Bdb8B74aa8c0C0baFC88592"


def _load_slots() -> dict:
    return json.loads(SLOTS_FILE.read_text())


def _menu(args: str = "") -> str:
    return (
        "ClawClinic commands\n\n"
        "/book - See available appointment slots and start x402 booking\n"
        "/insurance - List accepted insurance providers\n"
        "/insurance Sun Life - Check a named insurance provider\n"
        "/hours - Clinic hours and address\n"
        "/cancel - Cancel one booking or trigger guarded bulk cancellation\n"
        "/lookup BK-XXXXXXXX - Look up a booking from voice or Telegram\n"
        "/refill - Start a prescription refill request\n"
        "/restock - Autonomous supply restock via PharmaSupply (A2A x402)\n"
        "/onboard - View or change clinic configuration (spending limits, inventory, hours)\n"
        "/identity - Show ERC-8004 identity, wallet, and registry link\n\n"
        "High-risk actions require literal confirmations. Examples: CONFIRM CANCEL ALL, CONFIRM-ONBOARD-XXXXXX."
    )


def _install_telegram_menu_override() -> None:
    """Keep Telegram's slash picker focused on ClawClinic after restarts."""
    try:
        import hermes_cli.commands as commands
    except Exception:
        return

    def clawclinic_telegram_menu_commands(max_commands: int = 100):
        menu = [
            ("book", "Book an appointment with x402"),
            ("insurance", "Check accepted insurance"),
            ("hours", "Clinic hours and address"),
            ("cancel", "Cancel an appointment"),
            ("lookup", "Look up a booking by confirmation"),
            ("refill", "Request a prescription refill"),
            ("restock", "Autonomous A2A supply restock"),
            ("onboard", "Configure clinic limits and inventory"),
            ("identity", "Verify ERC-8004 identity"),
            ("menu", "Show ClawClinic commands"),
        ]
        return menu[:max_commands], max(0, len(menu) - max_commands)

    commands.telegram_menu_commands = clawclinic_telegram_menu_commands


# ─── static handlers ───────────────────────────────────────────────────

def _hours(args: str) -> str:
    d = _load_slots()
    h = d["hours"]
    return (
        f"🕒 {d['clinic']} — Hours\n"
        f"  Mon–Fri: {h['mon-fri']}\n"
        f"  Sat:     {h['sat']}\n"
        f"  Sun:     {h['sun']}\n\n"
        f"📍 {d['address']}"
    )


def _insurance(args: str) -> str:
    d = _load_slots()
    providers = d["insurance_accepted"]
    query = args.strip()
    if query:
        matched = next((p for p in providers if query.lower() in p.lower() or p.lower() in query.lower()), None)
        if matched:
            first_slot = next((s for s in d["slots"] if s["available"]), None)
            next_step = ""
            if first_slot:
                next_step = (
                    f"\n\nEarliest available covered visit: {first_slot['date']} "
                    f"{first_slot['time']} — {first_slot['service']}.\n"
                    "To book it, use /book and reply with slot 1 plus your wallet address."
                )
            return (
                f"Coverage check: {matched} is accepted at {d['clinic']}.\n"
                "Most cleanings, consults, and basic restorative visits can be submitted directly; "
                "final coverage depends on the patient's plan details."
                f"{next_step}"
            )
        return (
            f"I don't see {query} on the accepted list for {d['clinic']}.\n\n"
            "Accepted providers:\n"
            + "\n".join(f"  - {x}" for x in providers)
            + "\n\nI can still book the visit, but the clinic should confirm benefits before treatment."
        )
    lines = "\n".join(f"  - {x}" for x in providers)
    return (
        f"Accepted insurance:\n{lines}\n\n"
        "To check a plan, send /insurance Sun Life or /insurance Manulife."
    )


def _identity(args: str) -> str:
    return (
        "🪪 ClawClinic Identity\n"
        f"  Name:     ClawClinic\n"
        f"  Network:  GOAT Network\n"
        f"  Wallet:   {WALLET}\n"
        f"  Testnet AgentID (chain 48816): {TESTNET_AGENT_ID}\n"
        f"  Mainnet AgentID (chain 2345): {MAINNET_AGENT_ID}\n"
        f"  Registry: https://8004scan.io/agents/goat/{MAINNET_AGENT_ID}\n\n"
        "I'm an ERC-8004 agent with a verifiable on-chain identity. "
        "Clinics can confirm the wallet and agent record before paying."
    )


# ─── dynamic handlers ──────────────────────────────────────────────────

def _book(args: str) -> str:
    d = _load_slots()
    open_slots = [s for s in d["slots"] if s["available"]]
    if not open_slots:
        return "No open slots right now. Try again later or message us with a preferred time."

    lines = []
    for i, s in enumerate(open_slots, 1):
        lines.append(f"  [{i}] {s['date']} {s['time']} — {s['service']}  (id: {s['id']})")
    listing = "\n".join(lines)
    price = d.get("price_per_booking_usdc", 1.00)

    return (
        f"📅 Available slots at {d['clinic']}:\n{listing}\n\n"
        f"To book, reply with: <slot_number> <your_wallet_address>\n"
        f"Example:  1 0x9deEC91428b2637c9Bdb8B74aa8c0C0baFC88592\n\n"
        f"Fee: ${price:.2f} USDC, charged via x402 on GOAT Mainnet (chain 2345)."
    )


def _cancel(args: str) -> str:
    if not args.strip():
        return (
            "✂️  Cancel an appointment.\n\n"
            "Reply with your confirmation number (e.g. BK-12345678).\n"
            "For bulk cancellations (2+ appointments), I'll require an extra confirmation."
        )
    text = args.strip()
    if text.lower().startswith(("bk-", "bk_")):
        return f"Cancelled {text}. A confirmation has been logged."
    return (
        "I see you may be asking for a bulk cancellation. To proceed, reply with:\n"
        "  CONFIRM CANCEL ALL\n\n"
        "Or send a single confirmation number like `BK-12345678` to cancel just one."
    )


def _format_booking(row: dict) -> str:
    status = (row.get("status") or "unknown").strip()
    confirmation = (row.get("confirmation") or "BK-UNKNOWN").strip()
    return (
        f"📋 Booking {confirmation}\n"
        f"  Status:  {status}\n"
        f"  Time:    {row.get('slot_start','?')} to {row.get('slot_end','?')}\n"
        f"  Service: {row.get('service','?')}\n"
        f"  Patient: {row.get('patient_name','?')}\n"
        f"  Phone:   {row.get('caller_id') or 'not captured'}"
    )


def _lookup_booking(confirmation: str) -> str:
    needle = confirmation.strip().upper().replace("_", "-")
    if not needle:
        return "Usage: /lookup BK-XXXXXXXX"
    if not needle.startswith("BK-"):
        needle = f"BK-{needle}"
    if not VOICE_BOOKINGS_CSV.exists():
        return (
            "No shared booking history is available yet.\n"
            f"I looked for {VOICE_BOOKINGS_CSV}."
        )
    with VOICE_BOOKINGS_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("confirmation") or "").strip().upper() == needle:
                return _format_booking(row)
    return f"I couldn't find booking {needle} in the shared booking history."


def _lookup(args: str) -> str:
    return _lookup_booking(args)


def _refill(args: str) -> str:
    rx = args.strip()
    if rx:
        if rx.isdigit() and len(rx) == 6:
            return (
                f"Prescription {rx} found in the refill queue.\n\n"
                "To continue, reply with the wallet address paying the $1.00 USDC x402 refill processing fee."
            )
        return (
            "Prescription numbers at this pharmacy must be exactly 6 digits.\n"
            "Example: /refill 667689"
        )
    return (
        "💊 Prescription refill.\n\n"
        "Reply with your 6-digit prescription number to start. "
        "Fee: $1.00 USDC via x402 on GOAT Mainnet (chain 2345)."
    )


def _restock(args: str) -> str:
    """Run the autonomous A2A procurement client.

    Static handler: shells out to procurement_client.py and returns its full
    output. The first argument, if it looks like CONFIRM-RESTOCK-XXXXXX, is
    forwarded as the over-limit confirmation token.
    """
    if not PROCUREMENT_CLIENT.exists():
        return (
            "Restock is unavailable on this host.\n"
            "(procurement_client.py not found at the configured path)"
        )

    cmd = ["python3", str(PROCUREMENT_CLIENT), "restock"]
    token = args.strip().split()[0] if args.strip() else ""
    if token.upper().startswith("CONFIRM-RESTOCK-"):
        cmd += ["--confirm-token", token]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROCUREMENT_DIR),
            capture_output=True,
            text=True,
            timeout=240,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except subprocess.TimeoutExpired:
        return (
            "Restock timed out after 4 minutes.\n"
            "The on-chain transfer may still have broadcast — check the explorer "
            "before retrying."
        )

    body = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not body:
        return f"Restock finished with exit code {proc.returncode} and no output."
    return body[:3500]


# ─── /onboard — clinic configuration with literal-token guardrails ─────

def _load_clinic_config_module():
    """Import the in-project clinic_config module on demand.

    Kept lazy so a missing PROCUREMENT_DIR (e.g. someone else's checkout)
    does not break the plugin import — /onboard just becomes unavailable.
    """
    import sys
    if str(PROCUREMENT_DIR) not in sys.path:
        sys.path.insert(0, str(PROCUREMENT_DIR))
    import importlib
    try:
        mod = importlib.import_module("clinic_config")
        return importlib.reload(mod)
    except Exception:  # noqa: BLE001
        return None


_ONBOARD_HELP = (
    "🛠  ClawClinic onboarding — clinic configuration\n\n"
    "Read (always safe):\n"
    "  /onboard                       — show current config + 24h spend\n"
    "  /onboard pending               — list pending confirmation tokens\n\n"
    "Propose a change (returns a CONFIRM-ONBOARD-XXXXXX token):\n"
    "  /onboard set-limit <usd>                    — per-restock autonomous limit\n"
    "  /onboard set-daily <usd>                    — rolling 24h cumulative cap\n"
    "  /onboard set-clinic name|hours|address <value>\n"
    "  /onboard add-sku <SKU> <price> <threshold> <name…>\n"
    "  /onboard remove-sku <SKU>\n"
    "  /onboard reset                              — restore defaults\n\n"
    "Confirm or abort a pending proposal:\n"
    "  /onboard confirm <CONFIRM-ONBOARD-XXXXXX>\n"
    "  /onboard abort   <CONFIRM-ONBOARD-XXXXXX>\n\n"
    "Approximate confirmations (e.g. 'yes', 'do it') are rejected. "
    "The literal token must match exactly."
)


def _onboard_show_config(mod) -> str:
    cfg = mod.load_config()
    spent = mod.spend_last_24h()
    spending = cfg.get("spending", {})
    clinic = cfg.get("clinic", {})
    inv = cfg.get("inventory_config", {})

    lines = ["🛠  ClawClinic configuration\n"]
    lines.append("Clinic")
    lines.append(f"  name:    {clinic.get('name','—')}")
    lines.append(f"  hours:   {clinic.get('hours','—')}")
    lines.append(f"  address: {clinic.get('address','—')}")
    lines.append("")
    lines.append("Spending")
    lines.append(f"  per-restock autonomous limit: ${float(spending.get('autonomous_limit_usd',0)):.2f}")
    lines.append(f"  rolling 24h cumulative cap:   ${float(spending.get('daily_cap_usd',0)):.2f}")
    lines.append(f"  spent in last 24h:            ${spent:.2f}")
    lines.append("")
    lines.append("Inventory (thresholds + supplier prices)")
    if not inv:
        lines.append("  (none configured)")
    else:
        for sku, row in inv.items():
            lines.append(
                f"  {sku}: {row.get('name','?')} — threshold {row.get('threshold','?')}"
                f" @ ${float(row.get('unit_price_usd',0)):.2f}"
            )
    lines.append("")
    pending = mod.list_proposals()
    if pending:
        lines.append(f"⏳ Pending proposals: {len(pending)} (run /onboard pending)")
    else:
        lines.append("⏳ No pending proposals.")

    # Sub-action quick-reference so the operator doesn't have to remember the
    # surface. Reads are listed first since they're always safe.
    lines.append("")
    lines.append("Sub-actions")
    lines.append("  Read (free):")
    lines.append("    /onboard                 — show this view")
    lines.append("    /onboard pending         — list pending proposals")
    lines.append("    /onboard help            — full sub-action reference")
    lines.append("  Propose (returns a CONFIRM-ONBOARD-XXXXXX token):")
    lines.append("    /onboard set-limit <usd>                 — per-restock autonomous limit")
    lines.append("    /onboard set-daily <usd>                 — rolling 24h cumulative cap")
    lines.append("    /onboard set-clinic name|hours|address <value>")
    lines.append("    /onboard add-sku <SKU> <price> <threshold> <name…>")
    lines.append("    /onboard remove-sku <SKU>")
    lines.append("    /onboard reset")
    lines.append("  Apply / abort:")
    lines.append("    /onboard confirm <CONFIRM-ONBOARD-XXXXXX>")
    lines.append("    /onboard abort   <CONFIRM-ONBOARD-XXXXXX>")
    return "\n".join(lines)


def _onboard_show_pending(mod) -> str:
    pending = mod.list_proposals()
    if not pending:
        return "No pending proposals."
    lines = ["⏳ Pending proposals\n"]
    for token, prop in pending.items():
        lines.append(f"  {token}")
        lines.append(f"    action:  {prop.get('action')}")
        lines.append(f"    payload: {prop.get('payload')}")
        lines.append(f"    created: {prop.get('created_at')}")
        lines.append("")
    lines.append(
        "Apply with /onboard confirm <TOKEN>, or discard with /onboard abort <TOKEN>."
    )
    return "\n".join(lines)


def _onboard_apply_or_abort(mod, action: str, token: str) -> str:
    token = token.strip()
    if not token.startswith("CONFIRM-ONBOARD-"):
        return (
            "❌ That is not a valid confirmation token. Tokens look like "
            "CONFIRM-ONBOARD-AB12CD and are emitted by a propose command."
        )
    if action == "abort":
        ok = mod.abort_proposal(token)
        return f"✅ Proposal {token} aborted." if ok else f"❌ No pending proposal {token}."
    ok, msg, payload = mod.apply_proposal(token)
    if not ok:
        return f"❌ {msg}"
    return f"✅ {msg} ({payload})"


def _onboard_propose_set_limit(mod, args: list, field: str) -> str:
    if len(args) != 1:
        return f"Usage: /onboard set-{field.split('_')[0]} <usd>"
    try:
        value = float(args[0])
    except ValueError:
        return f"❌ Could not parse {args[0]!r} as a USD amount."
    err = mod.validate_spending(field, value)
    if err:
        return f"❌ {err}"
    token = mod.propose_change("set_spending", {"field": field, "value": value})
    current = float(mod.load_config().get("spending", {}).get(field, 0))
    return (
        f"📝 Proposed: change {field} from ${current:.2f} → ${value:.2f}.\n\n"
        f"To apply:  /onboard confirm {token}\n"
        f"To abort:  /onboard abort {token}"
    )


def _onboard_propose_set_clinic(mod, args: list) -> str:
    if len(args) < 2:
        return "Usage: /onboard set-clinic name|hours|address <value…>"
    field = args[0].lower()
    value = " ".join(args[1:]).strip()
    err = mod.validate_clinic(field, value)
    if err:
        return f"❌ {err}"
    token = mod.propose_change("set_clinic_field", {"field": field, "value": value})
    return (
        f"📝 Proposed: set clinic.{field} = {value!r}.\n\n"
        f"To apply:  /onboard confirm {token}\n"
        f"To abort:  /onboard abort {token}"
    )


def _onboard_propose_add_sku(mod, args: list) -> str:
    # Usage: /onboard add-sku <SKU> <price> <threshold> <name words…>
    if len(args) < 4:
        return "Usage: /onboard add-sku <SKU> <unit_price_usd> <threshold> <name…>"
    sku = args[0].strip().upper()
    try:
        price = float(args[1])
        threshold = int(args[2])
    except ValueError:
        return "❌ Price must be a number and threshold must be an integer."
    name = " ".join(args[3:]).strip()
    err = mod.validate_add_sku(sku, name, price, threshold)
    if err:
        return f"❌ {err}"
    token = mod.propose_change(
        "add_sku",
        {"sku": sku, "name": name, "unit_price_usd": price, "threshold": threshold},
    )
    return (
        f"📝 Proposed: add SKU {sku} ({name}) @ ${price:.2f}, threshold {threshold}.\n\n"
        f"To apply:  /onboard confirm {token}\n"
        f"To abort:  /onboard abort {token}"
    )


def _onboard_propose_remove_sku(mod, args: list) -> str:
    if len(args) != 1:
        return "Usage: /onboard remove-sku <SKU>"
    sku = args[0].strip().upper()
    cfg = mod.load_config()
    if sku not in cfg.get("inventory_config", {}):
        return f"❌ SKU {sku} is not in the current inventory config."
    token = mod.propose_change("remove_sku", {"sku": sku})
    return (
        f"📝 Proposed: remove SKU {sku}.\n\n"
        f"To apply:  /onboard confirm {token}\n"
        f"To abort:  /onboard abort {token}"
    )


def _onboard_propose_reset(mod) -> str:
    token = mod.propose_change("reset", {})
    return (
        "📝 Proposed: reset clinic configuration to defaults.\n\n"
        f"To apply:  /onboard confirm {token}\n"
        f"To abort:  /onboard abort {token}"
    )


def _onboard(args: str) -> str:
    """Strict-allowlist dispatcher. Unknown sub-actions return the help menu."""
    mod = _load_clinic_config_module()
    if mod is None:
        return (
            "Onboarding is unavailable on this host.\n"
            "(clinic_config module could not be loaded from the procurement repo)"
        )

    parts = (args or "").strip().split()
    if not parts:
        return _onboard_show_config(mod)

    head = parts[0].lower()
    rest = parts[1:]

    if head in {"help", "menu", "?"}:
        return _ONBOARD_HELP
    if head == "pending":
        return _onboard_show_pending(mod)
    if head == "confirm":
        if not rest:
            return "❌ Usage: /onboard confirm <CONFIRM-ONBOARD-XXXXXX>"
        return _onboard_apply_or_abort(mod, "confirm", rest[0])
    if head == "abort":
        if not rest:
            return "❌ Usage: /onboard abort <CONFIRM-ONBOARD-XXXXXX>"
        return _onboard_apply_or_abort(mod, "abort", rest[0])
    if head == "set-limit":
        return _onboard_propose_set_limit(mod, rest, "autonomous_limit_usd")
    if head == "set-daily":
        return _onboard_propose_set_limit(mod, rest, "daily_cap_usd")
    if head == "set-clinic":
        return _onboard_propose_set_clinic(mod, rest)
    if head == "add-sku":
        return _onboard_propose_add_sku(mod, rest)
    if head == "remove-sku":
        return _onboard_propose_remove_sku(mod, rest)
    if head == "reset":
        return _onboard_propose_reset(mod)

    return (
        f"❌ Unknown /onboard sub-action: {head!r}.\n\n"
        + _ONBOARD_HELP
    )


# ─── registration ──────────────────────────────────────────────────────

def register(ctx) -> None:
    _install_telegram_menu_override()
    ctx.register_command("menu",      handler=_menu,      description="Show ClawClinic commands")
    ctx.register_command("hours",     handler=_hours,     description="See clinic hours and address")
    ctx.register_command("insurance", handler=_insurance, description="Check accepted insurance providers")
    ctx.register_command("identity",  handler=_identity,  description="Show ClawClinic's on-chain identity (ERC-8004)")
    ctx.register_command("book",      handler=_book,      description="Request an appointment at the clinic")
    ctx.register_command("cancel",    handler=_cancel,    description="Cancel an appointment")
    ctx.register_command("lookup",    handler=_lookup,    description="Look up a booking by confirmation number")
    ctx.register_command("refill",    handler=_refill,    description="Request a prescription refill")
    ctx.register_command("restock",   handler=_restock,   description="Autonomous A2A supply restock via PharmaSupply")
    ctx.register_command("onboard",   handler=_onboard,   description="Configure clinic spending limits, inventory, and facts (with literal-token guardrails)")
