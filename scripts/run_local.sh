#!/usr/bin/env bash
# Bring up a local simulator + web UI with persistent data under ./var.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p var/sim var/data

echo "[1/3] starting source simulator on :8080"
.venv/bin/python -I -m annarchive simulator \
  --host 127.0.0.1 --port 8080 --sim-root var/sim >var/sim.log 2>&1 &
SIM_PID=$!
sleep 1

echo "[2/3] seeding fictional scenario"
.venv/bin/python -I -m annarchive seed --base-url http://127.0.0.1:8080 >/dev/null

echo "[3/3] starting archive web UI on :8000"
.venv/bin/python -I -m annarchive web \
  --host 127.0.0.1 --port 8000 --data var/data \
  --sim-url http://127.0.0.1:8080 >var/web.log 2>&1 &
WEB_PID=$!

echo
echo "simulator : http://127.0.0.1:8080   (control surface: /sim in the web UI)"
echo "archive UI: http://127.0.0.1:8000"
echo "PIDs: simulator=$SIM_PID web=$WEB_PID"
echo "stop with: kill $SIM_PID $WEB_PID"
wait
