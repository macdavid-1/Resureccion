#!/bin/sh
# Resurrección dev/preview server. Freebuff injects PORT; default 7860.
set -e
cd "$(dirname "$0")"
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
exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-7860}"
