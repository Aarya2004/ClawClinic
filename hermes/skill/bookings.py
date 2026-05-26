#!/usr/bin/env python3
"""Shared ClawClinic booking history lookup.

Voice and Telegram share the same CSV-backed demo booking store.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


BOOKINGS_CSV = Path(
    "/Users/aaryaprakash/Development/random_projs/GOAT-Hackathon-2026/voice/bookings.csv"
)


def normalize_confirmation(value: str) -> str:
    value = value.strip().upper().replace("_", "-")
    if value and not value.startswith("BK-"):
        value = f"BK-{value}"
    return value


def lookup_confirmation(confirmation: str) -> dict:
    needle = normalize_confirmation(confirmation)
    if not BOOKINGS_CSV.exists():
        return {
            "ok": False,
            "error": "missing_booking_store",
            "path": str(BOOKINGS_CSV),
            "message": "The shared booking history file does not exist yet.",
        }

    with BOOKINGS_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("confirmation") or "").strip().upper() == needle:
                return {"ok": True, "booking": row}

    return {
        "ok": False,
        "error": "not_found",
        "confirmation": needle,
        "message": f"No booking found for {needle}.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    lookup = sub.add_parser("lookup")
    lookup.add_argument("confirmation")
    args = parser.parse_args()

    if args.cmd == "lookup":
        print(json.dumps(lookup_confirmation(args.confirmation), indent=2))


if __name__ == "__main__":
    main()
