#!/usr/bin/env bash
# Launch the mcbot dashboard on port 21306 (behind the reverse proxy at
# minebot.dury.dev). Puts the framework (mcbot/) on the import path.
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="$(cd ../framework && pwd):${PYTHONPATH:-}"

# Load OIDC/session config (dashboard/.env, chmod 600).
if [ -f .env ]; then set -a; . ./.env; set +a; fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-21306}"

exec .venv/bin/uvicorn app:app --host "$HOST" --port "$PORT" --app-dir backend "$@"
