#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

BRIDGE_PID=""
MAIN_PID=""

cleanup() {
  echo "[stop] shutting down processes..."
  if [ -n "${MAIN_PID}" ] && kill -0 "${MAIN_PID}" 2>/dev/null; then
    kill "${MAIN_PID}" 2>/dev/null || true
  fi
  if [ -n "${BRIDGE_PID}" ] && kill -0 "${BRIDGE_PID}" 2>/dev/null; then
    kill "${BRIDGE_PID}" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}

trap cleanup SIGINT SIGTERM EXIT

echo "[start] openim sdk bridge"
npm run openim-bridge &
BRIDGE_PID=$!

# Give the bridge a short warm-up so health checks/login can start first.
sleep 1

echo "[start] python main"
python3 main.py &
MAIN_PID=$!

# If either process exits, stop the other and exit.
wait -n "${BRIDGE_PID}" "${MAIN_PID}"
