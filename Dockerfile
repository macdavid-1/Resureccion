# Resurrección — Hugging Face Spaces (Space SDK: docker)
#
# One image contains the whole system:
#   python3.11-slim  — FastAPI backend + research agent
#   nodejs           — scripts/render_pdf.js sidecar (PDFKit, report → 6×9in PDF)
#   playwright       — persistent Chromium with system deps + KDSpy extension support
#
# HF Spaces: the app MUST listen on 0.0.0.0:7860 and persistent storage is
# mounted at /data (Space setting "Persistent storage", mapped via DATA_DIR=/data).

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    # Uvicorn binding (HF Spaces requires 7860)
    HOST=0.0.0.0 \
    PORT=7860 \
    # Durable storage root — must be a mounted volume in the Space
    DATA_DIR=/data \
    # Chromium in a container: no user namespaces, no GPU. The config layer
    # auto-detects root/container and adds --no-sandbox; this makes it explicit.
    BROWSER_NO_SANDBOX=true \
    BROWSER_HEADLESS=true \
    # Playwright browser download location (kept in-image, not in /data: a
    # browser upgrade in a new image must not clash with old binaries in the bucket)
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# --- System packages -------------------------------------------------------
# Node 22 (PDFKit sidecar) + Playwright Chromium runtime libraries + fonts for
# non-Latin marketplaces (Amazon JP/DE/FR/IT/ES... render screenshots correctly).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
        fonts-liberation \
        fonts-noto-core \
        fonts-noto-cjk \
        # Playwright chromium dependencies
        libnss3 \
        libnspr4 \
        libdbus-1-3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libatspi2.0-0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpango-1.0-0 \
        libcairo2 \
    # Node.js 22 from NodeSource (pdfkit sidecar)
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python dependencies ----------------------------------------------------
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn>=0.29" \
    "pydantic>=2.6" \
    "python-multipart>=0.0.9" \
    "aiofiles>=23.2" \
    "httpx>=0.27" \
    "pillow>=10.0" \
    "imageio>=2.34" \
    "imageio-ffmpeg>=0.4.9" \
    "playwright>=1.42"

# --- Chromium for Playwright (with system deps) -----------------------------
RUN playwright install --with-deps chromium

# --- Node sidecar dependencies ----------------------------------------------
COPY package.json ./
RUN npm install --no-audit --no-fund --omit=dev

# --- Application -------------------------------------------------------------
COPY app/ ./app/
COPY scripts/render_pdf.js ./scripts/render_pdf.js
COPY static/ ./static/
COPY run.sh ./run.sh
RUN chmod +x run.sh

# --- Non-root runtime user ---------------------------------------------------
# HF Spaces runs containers as user 1000 by default; files written to /data
# must be owned by that uid. Matches the Space's default user.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app

USER appuser

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/api/health || exit 1

CMD ["./run.sh"]
