"""Clinic configuration — single source of truth for ClawClinic's
operational parameters that the clinic can change at runtime via
/onboard.

The on-disk file is `clinic_config.json`. If it does not exist yet,
`clinic_config.default.json` (which IS tracked in git) is used as the
seed. Writes are atomic via `os.replace`.

Design notes
------------
- All getters return deep copies so callers cannot mutate the cached
  state in place.
- All write paths funnel through `propose_change()` / `apply_change()`
  so the literal-confirmation-token gate is enforced uniformly.
- The spending guardrail tracks two windows: per-restock (hard cap on
  any single autonomous transfer) and a rolling 24h cumulative cap.
- The audit log is append-only. Each proposal, confirmation, abort,
  and applied change is recorded with a timestamp so judges and
  operators can review what the bot did and when.
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "clinic_config.json"
DEFAULT_CONFIG_PATH = HERE / "clinic_config.default.json"
PROPOSALS_PATH = HERE / ".onboard_proposals.json"
SPEND_LEDGER_PATH = HERE / ".spend_ledger.json"
AUDIT_LOG_PATH = HERE / ".onboard_audit.log"

# Hard ceilings the runtime will never accept regardless of operator input.
HARD_CEILINGS = {
    "autonomous_limit_usd": 1000.00,
    "daily_cap_usd": 10000.00,
    "threshold": 100000,
    "unit_price_usd": 10000.00,
}

CLINIC_FIELDS_WRITABLE = {"name", "hours", "address"}
SPENDING_FIELDS_WRITABLE = {"autonomous_limit_usd", "daily_cap_usd"}


# ─── low-level io ──────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _append_audit(event: str, detail: dict) -> None:
    line = json.dumps({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        **detail,
    })
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(line + "\n")


# ─── config getters ────────────────────────────────────────────────────

def _load_default() -> dict:
    d = _read_json(DEFAULT_CONFIG_PATH, default=None)
    if d is None:
        raise RuntimeError(
            f"missing default config at {DEFAULT_CONFIG_PATH}"
        )
    return d


def load_config() -> dict:
    """Return the current config (a deep copy).

    First call seeds the on-disk config from the default if it does not
    yet exist, so subsequent reads + writes share the same file.
    """
    if not CONFIG_PATH.exists():
        _atomic_write_json(CONFIG_PATH, _load_default())
    return copy.deepcopy(_read_json(CONFIG_PATH, default=_load_default()))


def autonomous_limit_usd() -> float:
    return float(load_config().get("spending", {}).get("autonomous_limit_usd", 5.0))


def daily_cap_usd() -> float:
    return float(load_config().get("spending", {}).get("daily_cap_usd", 50.0))


def inventory_config() -> dict:
    return copy.deepcopy(load_config().get("inventory_config", {}))


def clinic_facts() -> dict:
    return copy.deepcopy(load_config().get("clinic", {}))


# ─── spend ledger (rolling 24h) ────────────────────────────────────────

def _now_ts() -> float:
    return time.time()


def _read_ledger() -> list:
    return _read_json(SPEND_LEDGER_PATH, default=[]) or []


def _write_ledger(rows: list) -> None:
    _atomic_write_json(SPEND_LEDGER_PATH, rows)


def record_spend(amount_usd: float, tx_hash: str, sku: str) -> None:
    rows = _read_ledger()
    rows.append({
        "ts": _now_ts(),
        "amount_usd": float(amount_usd),
        "tx_hash": tx_hash,
        "sku": sku,
    })
    _write_ledger(rows)


def spend_last_24h() -> float:
    cutoff = _now_ts() - 86400
    rows = [r for r in _read_ledger() if r.get("ts", 0) >= cutoff]
    return round(sum(float(r.get("amount_usd", 0)) for r in rows), 4)


# ─── proposal lifecycle ────────────────────────────────────────────────

def _read_proposals() -> dict:
    return _read_json(PROPOSALS_PATH, default={}) or {}


def _write_proposals(d: dict) -> None:
    _atomic_write_json(PROPOSALS_PATH, d)


def make_proposal_token() -> str:
    return f"CONFIRM-ONBOARD-{uuid.uuid4().hex[:6].upper()}"


def propose_change(action: str, payload: dict) -> str:
    """Stage a destructive change behind a literal confirmation token.

    Returns the token. The change is NOT applied until apply_proposal()
    is called with the same token. Tokens are single-use.
    """
    token = make_proposal_token()
    proposals = _read_proposals()
    proposals[token] = {
        "action": action,
        "payload": payload,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_proposals(proposals)
    _append_audit("propose", {"token": token, "action": action, "payload": payload})
    return token


def list_proposals() -> dict:
    return copy.deepcopy(_read_proposals())


def abort_proposal(token: str) -> bool:
    """Remove a pending proposal without applying it."""
    proposals = _read_proposals()
    if token not in proposals:
        return False
    detail = proposals.pop(token)
    _write_proposals(proposals)
    _append_audit("abort", {"token": token, **detail})
    return True


def apply_proposal(token: str) -> tuple[bool, str, dict | None]:
    """Apply a pending proposal. Returns (ok, message, applied_payload)."""
    proposals = _read_proposals()
    prop = proposals.get(token)
    if prop is None:
        return False, "Unknown or already-used confirmation token.", None

    action = prop["action"]
    payload = prop["payload"]

    cfg = load_config()
    cfg = _apply_to_config(cfg, action, payload)
    if cfg is None:
        return False, f"Could not apply {action}: payload no longer valid.", None

    _atomic_write_json(CONFIG_PATH, cfg)
    del proposals[token]
    _write_proposals(proposals)
    _append_audit("apply", {"token": token, "action": action, "payload": payload})
    return True, f"Applied {action}.", payload


def _apply_to_config(cfg: dict, action: str, payload: dict) -> dict | None:
    """Pure-function applier — does not touch disk."""
    if action == "set_clinic_field":
        field = payload.get("field")
        value = payload.get("value")
        if field not in CLINIC_FIELDS_WRITABLE or not isinstance(value, str):
            return None
        cfg.setdefault("clinic", {})[field] = value.strip()
        return cfg

    if action == "set_spending":
        field = payload.get("field")
        value = payload.get("value")
        if field not in SPENDING_FIELDS_WRITABLE:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if value < 0 or value > HARD_CEILINGS[field]:
            return None
        cfg.setdefault("spending", {})[field] = round(value, 2)
        return cfg

    if action == "add_sku":
        sku = (payload.get("sku") or "").strip().upper()
        name = (payload.get("name") or "").strip()
        try:
            threshold = int(payload.get("threshold"))
            unit_price = float(payload.get("unit_price_usd"))
        except (TypeError, ValueError):
            return None
        if not sku or not name or threshold <= 0 or unit_price <= 0:
            return None
        if threshold > HARD_CEILINGS["threshold"] or unit_price > HARD_CEILINGS["unit_price_usd"]:
            return None
        cfg.setdefault("inventory_config", {})[sku] = {
            "name": name,
            "threshold": threshold,
            "unit_price_usd": round(unit_price, 2),
        }
        return cfg

    if action == "remove_sku":
        sku = (payload.get("sku") or "").strip().upper()
        if not sku or sku not in cfg.get("inventory_config", {}):
            return None
        del cfg["inventory_config"][sku]
        return cfg

    if action == "reset":
        return _load_default()

    return None


# ─── validation helpers used before staging a proposal ─────────────────

def validate_spending(field: str, value: float) -> str | None:
    if field not in SPENDING_FIELDS_WRITABLE:
        return f"Unknown spending field. Allowed: {', '.join(sorted(SPENDING_FIELDS_WRITABLE))}."
    if value < 0:
        return "Spending limits must be non-negative."
    ceiling = HARD_CEILINGS.get(field)
    if ceiling is not None and value > ceiling:
        return f"{field} cannot exceed ${ceiling:.2f} (hard ceiling)."
    return None


def validate_clinic(field: str, value: str) -> str | None:
    if field not in CLINIC_FIELDS_WRITABLE:
        return f"Unknown clinic field. Allowed: {', '.join(sorted(CLINIC_FIELDS_WRITABLE))}."
    if not value or len(value) > 200:
        return "Value must be 1–200 characters."
    return None


def validate_add_sku(sku: str, name: str, unit_price_usd: float, threshold: int) -> str | None:
    if not sku or len(sku) > 40:
        return "SKU must be 1–40 characters."
    if not name or len(name) > 120:
        return "Item name must be 1–120 characters."
    if unit_price_usd <= 0 or unit_price_usd > HARD_CEILINGS["unit_price_usd"]:
        return f"Unit price must be >$0 and ≤ ${HARD_CEILINGS['unit_price_usd']:.2f}."
    if threshold <= 0 or threshold > HARD_CEILINGS["threshold"]:
        return f"Threshold must be a positive integer ≤ {HARD_CEILINGS['threshold']}."
    return None
