#!/usr/bin/env bash
# Launch the mcbot bot-host on port 21307 (localhost only). This is the
# long-lived process that owns the bots; the dashboard (run.sh) proxies to it.
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="$(cd ../framework && pwd):${PYTHONPATH:-}"

HOST="${HOST:-127.0.0.1}"
PORT="${BOTHOST_PORT:-21307}"

exec .venv/bin/uvicorn host:app --host "$HOST" --port "$PORT" --app-dir backend "$@"
