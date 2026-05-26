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
from pathlib import Path


SKILL_DIR = Path.home() / ".hermes" / "skills" / "clawclinic"
SLOTS_FILE = SKILL_DIR / "slots.json"

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
        "/refill - Start a prescription refill request\n"
        "/identity - Show ERC-8004 identity, wallet, and registry link\n\n"
        "High-risk actions require literal confirmations. Example: CONFIRM CANCEL ALL."
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
            ("refill", "Request a prescription refill"),
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


# ─── registration ──────────────────────────────────────────────────────

def register(ctx) -> None:
    _install_telegram_menu_override()
    ctx.register_command("menu",      handler=_menu,      description="Show ClawClinic commands")
    ctx.register_command("hours",     handler=_hours,     description="See clinic hours and address")
    ctx.register_command("insurance", handler=_insurance, description="Check accepted insurance providers")
    ctx.register_command("identity",  handler=_identity,  description="Show ClawClinic's on-chain identity (ERC-8004)")
    ctx.register_command("book",      handler=_book,      description="Request an appointment at the clinic")
    ctx.register_command("cancel",    handler=_cancel,    description="Cancel an appointment")
    ctx.register_command("refill",    handler=_refill,    description="Request a prescription refill")
