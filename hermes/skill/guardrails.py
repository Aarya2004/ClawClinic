#!/usr/bin/env python3
"""
ClawClinic guardrails — hard-gated checks for high-risk actions.

The agent MUST call this before any action in HIGH_RISK_ACTIONS.
Returns exit code 0 = proceed, non-zero = halt and surface the message.

Usage:
    python guardrails.py check --action bulk_cancel --count 12
    python guardrails.py check --action large_payment --amount-usdc 7.50
    python guardrails.py confirm --action bulk_cancel --token "CONFIRM CANCEL ALL"

Why this exists:
    LLMs can rationalize past free-text instructions. A script gate that
    requires a literal confirmation string can't be talked out of.
"""
import argparse
import json
import sys

HIGH_RISK_ACTIONS = {
    "bulk_cancel":     {"token": "CONFIRM CANCEL ALL",   "reason": "Cancelling multiple appointments at once"},
    "pricing_change":  {"token": "CONFIRM PRICING",      "reason": "Changing clinic pricing or fees"},
    "large_payment":   {"token": "CONFIRM PAY",          "reason": "x402 payment above $5 USDC"},
    "bulk_reschedule": {"token": "CONFIRM RESCHEDULE",   "reason": "Rescheduling multiple appointments at once"},
    "config_change":   {"token": "CONFIRM CONFIG",       "reason": "Modifying clinic configuration"},
}

LARGE_PAYMENT_THRESHOLD_USDC = 5.0
BULK_THRESHOLD = 2  # 2 or more = bulk


def check(args):
    action = args.action
    if action not in HIGH_RISK_ACTIONS:
        # Not in our risk list — proceed silently.
        print(json.dumps({"proceed": True, "action": action}))
        return 0

    rule = HIGH_RISK_ACTIONS[action]

    # Action-specific threshold checks (skip the gate for safe sub-thresholds).
    if action == "large_payment":
        if args.amount_usdc is None:
            sys.exit("guardrails: --amount-usdc required for large_payment")
        if args.amount_usdc <= LARGE_PAYMENT_THRESHOLD_USDC:
            print(json.dumps({"proceed": True, "action": action, "amount_usdc": args.amount_usdc}))
            return 0
    elif action in ("bulk_cancel", "bulk_reschedule"):
        if args.count is None:
            sys.exit(f"guardrails: --count required for {action}")
        if args.count < BULK_THRESHOLD:
            print(json.dumps({"proceed": True, "action": action, "count": args.count}))
            return 0

    # Above threshold — gate.
    msg = {
        "proceed": False,
        "action": action,
        "reason": rule["reason"],
        "required_confirmation": rule["token"],
        "user_facing_message": (
            f"⚠️  High-risk action: {rule['reason']}.\n"
            f"   To proceed, reply with: {rule['token']}\n"
            f"   To cancel, reply: nevermind"
        ),
    }
    print(json.dumps(msg, indent=2))
    return 2  # halt


def confirm(args):
    action = args.action
    if action not in HIGH_RISK_ACTIONS:
        sys.exit(f"guardrails: unknown action '{action}'")
    expected = HIGH_RISK_ACTIONS[action]["token"]
    given = (args.token or "").strip()
    if given == expected:
        print(json.dumps({"confirmed": True, "action": action}))
        return 0
    print(json.dumps({
        "confirmed": False,
        "action": action,
        "expected": expected,
        "given": given,
        "user_facing_message": f"Confirmation does not match. Expected exactly: {expected}",
    }, indent=2))
    return 3


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="Check whether an action is allowed")
    c.add_argument("--action", required=True, choices=HIGH_RISK_ACTIONS.keys())
    c.add_argument("--count", type=int, help="For bulk_*")
    c.add_argument("--amount-usdc", type=float, help="For large_payment")

    f = sub.add_parser("confirm", help="Verify a user-supplied confirmation string")
    f.add_argument("--action", required=True, choices=HIGH_RISK_ACTIONS.keys())
    f.add_argument("--token", required=True, help="The literal confirmation string from the user")

    args = p.parse_args()
    if args.cmd == "check":
        sys.exit(check(args))
    else:
        sys.exit(confirm(args))


if __name__ == "__main__":
    main()
