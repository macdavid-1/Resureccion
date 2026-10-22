#!/bin/sh
# Resurrección container entrypoint:
#   1. (optional) enable a swap file INSIDE the container so a memory spike in
#      the research browser is absorbed by swap on the container's own disk
#      instead of an OOM kill. The file lives in the container's writable
#      layer, so it follows the container's lifecycle and its SSD allocation —
#      it is never part of the host's own swap.
#      Needs --cap-add SYS_ADMIN (see docker-compose.yml); without it the
#      kernel refuses and the app starts anyway. Non-fatal by design.
#   2. drop from root to the unprivileged runtime user and exec the app.
set -e

SWAPFILE="${SWAPFILE:-/swapfile}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-2048}"

if [ "${SWAP_ENABLED:-1}" = "1" ] && [ "$(id -u)" = "0" ]; then
    if ! swapon --show=NAME --noheadings 2>/dev/null | grep -q "^${SWAPFILE}$"; then
        if [ ! -f "$SWAPFILE" ]; then
            echo "[entrypoint] creating ${SWAP_SIZE_MB}M swap file at $SWAPFILE"
            fallocate -l "${SWAP_SIZE_MB}M" "$SWAPFILE" || rm -f "$SWAPFILE"
            if [ -f "$SWAPFILE" ]; then
                chmod 600 "$SWAPFILE"
                mkswap "$SWAPFILE" >/dev/null 2>&1 || true
            fi
        fi
    fi
    if [ -f "$SWAPFILE" ] && swapon "$SWAPFILE" 2>/dev/null; then
        echo "[entrypoint] swap enabled: $SWAPFILE (${SWAP_SIZE_MB}M)"
    else
        echo "[entrypoint] swap NOT enabled — run the container with --cap-add SYS_ADMIN to get it; continuing without" >&2
    fi
fi

# Drop privileges when started as root (VPS / docker compose). Hugging Face
# Spaces and the Freebuff preview already run as uid 1000 and pass through.
if [ "$(id -u)" = "0" ]; then
    if command -v runuser >/dev/null 2>&1; then
        exec runuser -u appuser -- "$@"
    fi
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid=1000 --regid=1000 --init-groups "$@"
    fi
    echo "[entrypoint] cannot drop privileges (no runuser/setpriv); running as root" >&2
fi

exec "$@"
