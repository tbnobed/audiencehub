#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export APP_ENV="${APP_ENV:-development}"
export AUTH_MODE="${AUTH_MODE:-dev}"
export GMAIL_STYLE_DOMAINS="${GMAIL_STYLE_DOMAINS:-gmail.com,googlemail.com,gmail.test}"
if [ "$APP_ENV" = production ]; then
  echo "scripts/dev.sh must not run in production" >&2
  exit 1
fi
if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL is required" >&2
  exit 1
fi
# Derive missing development keys in-process from a stable SESSION_SECRET.
# No key material is printed, shell-evaluated, or written to disk.
export PYTHONPATH="$PWD/backend"
if [ -z "${SECRET_KEY:-}" ] || [ -z "${FERNET_KEY:-}" ] || [ -z "${PII_HASH_PEPPER:-}" ]; then
  exec python3 -m app.dev_bootstrap "$PWD/scripts/dev.sh" "$@"
fi
export UPLOAD_DIR="${UPLOAD_DIR:-$PWD/.data/uploads}"
export EXPORT_DIR="${EXPORT_DIR:-$PWD/.data/exports}"
# The seed CLI writes to $UPLOAD_DIR/seed-data, including when installed.
mkdir -p "$UPLOAD_DIR/seed-data" "$EXPORT_DIR"
export PYTHONPATH="$PWD/backend"
cd backend
python3 -m alembic upgrade head
python3 -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 &
api_pid=$!
python3 -m app.worker &
worker_pid=$!
cd ..
cleanup() { kill "$api_pid" "$worker_pid" 2>/dev/null || true; wait "$api_pid" "$worker_pid" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
pnpm exec vite --host 0.0.0.0 --port "${PORT:-5000}"