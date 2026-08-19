#!/usr/bin/env bash
set -euo pipefail

PLATFORM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PLATFORM_DIR"

HOST="${SCORING_HOST:-0.0.0.0}"
PORT="${SCORING_PORT:-8010}"

if command -v conda >/dev/null 2>&1 && conda env list | grep -q '^env_robfit '; then
  exec conda run --no-capture-output -n env_robfit uvicorn backend:app --host "$HOST" --port "$PORT"
fi

exec python3 -m uvicorn backend:app --host "$HOST" --port "$PORT"

