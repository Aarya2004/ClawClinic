#!/usr/bin/env bash
# Start the ClawClinic voice proxy.
#
# Prereqs:
#   1. Hermes gateway running with API_SERVER_ENABLED=true in ~/.hermes/.env
#      (defaults to listening on http://127.0.0.1:8642).
#   2. ngrok installed: `brew install ngrok` (or download).
#
# Usage:
#   ./run.sh           # start the proxy (foreground)
#   ./run.sh tunnel    # start the proxy AND open an ngrok tunnel on :8643
set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" == "tunnel" ]]; then
  python3 voice_proxy.py &
  PROXY_PID=$!
  trap "kill $PROXY_PID 2>/dev/null || true" EXIT
  sleep 1
  echo "proxy running (PID $PROXY_PID). starting ngrok on :8643..."
  exec ngrok http 8643
else
  exec python3 voice_proxy.py
fi
