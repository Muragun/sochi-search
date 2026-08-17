#!/usr/bin/env bash
set -euo pipefail

cd /opt/sochi-search
source .venv/bin/activate
set -a
source .env
set +a

exec uvicorn app.api:app --host "$APP_HOST" --port "$APP_PORT"
