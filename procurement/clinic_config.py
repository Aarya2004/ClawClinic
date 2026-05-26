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

import base64
import copy
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
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
OPERATOR_FIELDS_WRITABLE = {"phone_e164", "sms_required"}

# Twilio sender — single number for the demo, kept out of config.json so the
# operator can't accidentally divert OTPs to an attacker-controlled "from".
SMS_FROM_NUMBER = "+16477244594"
SMS_OTP_TTL_SECONDS = 600  # 10 min
SMS_MAX_ATTEMPTS = 5


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


def _make_otp() -> str:
    """Six-digit numeric OTP. Uniform across 000000-999999."""
    return f"{random.SystemRandom().randint(0, 999_999):06d}"


def operator_config() -> dict:
    return copy.deepcopy(load_config().get("operator", {}))


def _twilio_send_sms(to_e164: str, body: str) -> tuple[bool, str]:
    """Send an SMS via the Twilio REST API. Returns (ok, info)."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        return False, "Twilio credentials not configured (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN missing)"
    if not to_e164:
        return False, "Operator phone number is not configured"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({
        "From": SMS_FROM_NUMBER,
        "To": to_e164,
        "Body": body,
    }).encode()
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
            return False, f"Twilio error {err.get('code')}: {err.get('message')}"
        except Exception:
            return False, f"Twilio HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"Twilio unreachable: {e}"
    return True, resp.get("sid", "ok")


def propose_change(action: str, payload: dict) -> tuple[str, dict]:
    """Stage a destructive change behind a literal confirmation token.

    Returns (token, otp_status_dict). The change is NOT applied until
    apply_proposal() is called with the same token. Tokens are single-use.

    If operator.phone_e164 is set, an OTP is generated and sent via SMS;
    apply_proposal() then requires both the token AND the OTP unless
    operator.sms_required is false and the SMS send failed (graceful
    degradation: a literal-token-only confirmation is still accepted).
    """
    token = make_proposal_token()
    proposals = _read_proposals()
    record: dict[str, Any] = {
        "action": action,
        "payload": payload,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Generate + send OTP if operator phone is configured
    op = operator_config()
    phone = (op.get("phone_e164") or "").strip()
    sms_required = bool(op.get("sms_required", False))
    otp_status = {"sms_sent": False, "sms_required": sms_required, "phone_masked": ""}

    if phone:
        otp = _make_otp()
        record["otp_hash"] = otp  # plaintext is fine for this scope (single host, gitignored)
        record["otp_expires_at"] = time.time() + SMS_OTP_TTL_SECONDS
        record["otp_attempts"] = 0
        ok, info = _twilio_send_sms(
            phone,
            f"ClawClinic confirmation code: {otp}\n"
            f"Token: {token}\n"
            f"Action: {action}\n"
            f"This code expires in {SMS_OTP_TTL_SECONDS // 60} minutes.",
        )
        otp_status["sms_sent"] = ok
        otp_status["sms_info"] = info
        otp_status["phone_masked"] = phone[:3] + "***" + phone[-4:]
        if not ok and sms_required:
            # Strict mode: bail without persisting a proposal
            _append_audit(
                "propose_failed_sms",
                {"action": action, "payload": payload, "sms_info": info},
            )
            return token, {**otp_status, "ok": False, "error": "sms_required_but_failed"}

    proposals[token] = record
    _write_proposals(proposals)
    _append_audit("propose", {
        "token": token,
        "action": action,
        "payload": payload,
        "sms_sent": otp_status["sms_sent"],
    })
    otp_status["ok"] = True
    return token, otp_status


def list_proposals() -> dict:
    out = {}
    for token, prop in _read_proposals().items():
        # Never surface the otp_hash through list_proposals; clone without it.
        clean = {k: v for k, v in prop.items() if k != "otp_hash"}
        out[token] = clean
    return out


def abort_proposal(token: str) -> bool:
    """Remove a pending proposal without applying it."""
    proposals = _read_proposals()
    if token not in proposals:
        return False
    detail = {k: v for k, v in proposals.pop(token).items() if k != "otp_hash"}
    _write_proposals(proposals)
    _append_audit("abort", {"token": token, **detail})
    return True


def apply_proposal(token: str, otp: str | None = None) -> tuple[bool, str, dict | None]:
    """Apply a pending proposal.

    Returns (ok, message, applied_payload).
    - If the proposal carries an otp_hash and operator.sms_required is true,
      OTP must match. Wrong OTP increments otp_attempts; after SMS_MAX_ATTEMPTS
      the proposal is auto-aborted.
    - If sms_required is false, a missing OTP is allowed (fallback path) but
      an OTP that IS provided must still match if one was generated.
    """
    proposals = _read_proposals()
    prop = proposals.get(token)
    if prop is None:
        return False, "Unknown or already-used confirmation token.", None

    sms_required = bool(operator_config().get("sms_required", False))
    expected_otp = prop.get("otp_hash")

    if expected_otp:
        # OTP was generated for this proposal
        expires = prop.get("otp_expires_at", 0)
        if time.time() > expires:
            del proposals[token]
            _write_proposals(proposals)
            _append_audit("apply_failed_expired", {"token": token})
            return False, "Confirmation code has expired. Please /onboard set-... again.", None

        if otp is None:
            if sms_required:
                return False, (
                    "A 6-digit SMS code was sent to your phone. "
                    "Reply with:  /onboard confirm <TOKEN> <6-digit-code>"
                ), None
            # Fallback: literal-only when sms_required is false. Proceed.
        else:
            otp_clean = (otp or "").strip().replace("-", "").replace(" ", "")
            if otp_clean != expected_otp:
                prop["otp_attempts"] = int(prop.get("otp_attempts", 0)) + 1
                if prop["otp_attempts"] >= SMS_MAX_ATTEMPTS:
                    del proposals[token]
                    _write_proposals(proposals)
                    _append_audit(
                        "apply_failed_lockout",
                        {"token": token, "attempts": prop["otp_attempts"]},
                    )
                    return False, (
                        f"Too many incorrect codes ({SMS_MAX_ATTEMPTS}). "
                        "Proposal cancelled — please /onboard set-... again."
                    ), None
                proposals[token] = prop
                _write_proposals(proposals)
                _append_audit(
                    "apply_failed_bad_otp",
                    {"token": token, "attempts": prop["otp_attempts"]},
                )
                return False, (
                    f"Incorrect code ({prop['otp_attempts']}/{SMS_MAX_ATTEMPTS} attempts). "
                    "Please re-enter the 6-digit SMS code."
                ), None

    elif sms_required:
        # sms_required is true but no OTP was generated for this proposal —
        # probably created when SMS was failing. Refuse.
        return False, (
            "This proposal has no SMS code on file but sms_required is true. "
            "Please /onboard set-... again."
        ), None

    action = prop["action"]
    payload = prop["payload"]

    cfg = load_config()
    cfg = _apply_to_config(cfg, action, payload)
    if cfg is None:
        return False, f"Could not apply {action}: payload no longer valid.", None

    _atomic_write_json(CONFIG_PATH, cfg)
    del proposals[token]
    _write_proposals(proposals)
    _append_audit("apply", {
        "token": token,
        "action": action,
        "payload": payload,
        "used_otp": expected_otp is not None and otp is not None,
    })
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

    if action == "set_operator":
        field = payload.get("field")
        value = payload.get("value")
        if field not in OPERATOR_FIELDS_WRITABLE:
            return None
        if field == "phone_e164":
            if not isinstance(value, str) or not value.startswith("+") or not value[1:].isdigit():
                return None
            if len(value) > 16:
                return None
        elif field == "sms_required":
            if not isinstance(value, bool):
                return None
        cfg.setdefault("operator", {})[field] = value
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


def validate_operator(field: str, value: Any) -> str | None:
    if field not in OPERATOR_FIELDS_WRITABLE:
        return f"Unknown operator field. Allowed: {', '.join(sorted(OPERATOR_FIELDS_WRITABLE))}."
    if field == "phone_e164":
        if not isinstance(value, str) or not value.startswith("+") or not value[1:].isdigit():
            return "Phone must be in E.164 format, e.g. +14165550123."
        if len(value) < 8 or len(value) > 16:
            return "Phone number length looks wrong (expected 8–16 chars including the +)."
    elif field == "sms_required":
        if not isinstance(value, bool):
            return "sms_required must be true or false."
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
