#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
mkdir -p .local data
if [ ! -s .local/admin-key ]; then
  .venv/bin/python -c 'from pathlib import Path; import secrets; Path(".local/admin-key").write_text(secrets.token_urlsafe(32), encoding="utf-8")'
fi
if [ ! -s .local/join-key ]; then
  .venv/bin/python -c 'from pathlib import Path; import secrets; Path(".local/join-key").write_text(secrets.token_urlsafe(16), encoding="utf-8")'
fi
export MAILBOX_ADMIN_KEY="$(cat .local/admin-key)"
export MAILBOX_JOIN_KEY="$(cat .local/join-key)"
export MAILBOX_STORAGE=sqlite
export MAILBOX_LOCAL_FIRST_RUN=1
export MAILBOX_DB=data/mailbox.sqlite3
unset MAILBOX_PUBLIC_URL MAILBOX_MCP_ALLOWED_HOSTS
exec .venv/bin/python -m uvicorn server:create_app --factory --host 127.0.0.1 --port "${PORT:-8795}"
