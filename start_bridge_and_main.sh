#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f .env ]]; then
  echo "[warn] .env not found. Copy .env.sample to .env and fill required values first."
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[error] npm not found. Install Node.js first."
  exit 1
fi

if ! command -v python >/dev/null 2>&1; then
  echo "[error] python not found. Install Python first."
  exit 1
fi

cleanup() {
  echo ""
  echo "[stop] shutting down bridge/main..."
  if [[ -n "${BRIDGE_PID:-}" ]] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    kill "$BRIDGE_PID" 2>/dev/null || true
  fi
  if [[ -n "${MAIN_PID:-}" ]] && kill -0 "$MAIN_PID" 2>/dev/null; then
    kill "$MAIN_PID" 2>/dev/null || true
  fi
}

trap cleanup INT TERM EXIT

echo "[start] starting OpenIM bridge..."
npm run openim-bridge &
BRIDGE_PID=$!

sleep 1

echo "[start] starting main.py..."
python main.py &
MAIN_PID=$!

echo "[ok] bridge PID=$BRIDGE_PID"
echo "[ok] main   PID=$MAIN_PID"
echo "[hint] press Ctrl+C to stop both"

wait "$BRIDGE_PID" "$MAIN_PID"
