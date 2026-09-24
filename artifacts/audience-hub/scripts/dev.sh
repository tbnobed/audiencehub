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
# Development-only ephemeral secrets; never use these in production.
export SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import secrets;print(secrets.token_hex(32))')}"
export FERNET_KEY="${FERNET_KEY:-$(python3 -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')}"
export PII_HASH_PEPPER="${PII_HASH_PEPPER:-$(python3 -c 'import secrets;print(secrets.token_hex(32))')}"
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