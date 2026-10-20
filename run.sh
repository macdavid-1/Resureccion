#!/bin/sh
# Resurrección server — one boot path for both environments:
#   * the Freebuff preview (environment injected by the platform), and
#   * a standalone VPS under pm2 (see ecosystem.config.cjs and VPS.md).
set -e
cd "$(dirname "$0")"

# On a VPS nothing injects the environment, so the owner keeps values in
# .env. Loading it here keeps run.sh the single boot path everywhere: the
# file is authoritative for anything it defines, and is never printed.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# Use the project-local virtualenv when present.
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

# Node sidecar deps (PDFKit for PDF export) — idempotent, skipped when present.
if [ -f package.json ] && [ ! -d node_modules/pdfkit ]; then
  npm install --no-audit --no-fund || echo "[run.sh] npm install failed; PDF export disabled" >&2
fi

# Camoufox browser binary — idempotent, and non-fatal: without it the app
# still boots and the UI still works, only browser launches fail. A fresh VPS
# has no cache, which is why this runs before uvicorn instead of at first use.
"$PY" scripts/camoufox_ensure.py \
  || echo "[run.sh] camoufox browser unavailable — browser launches will fail until it is fetched" >&2

exec "$PY" -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-7860}"
