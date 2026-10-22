#!/bin/sh
# Sleep Resurrección: a graceful stop that releases this container's CPU and
# RAM so other apps on the VPS can use them. The SSD is untouched — the
# container, its /data volume (SQLite DB, artifacts, browser profile) and the
# 2 GB swap file all stay exactly where they are, ready for `host_awake.sh`.
#
# On the host:  sh scripts/host_sleep.sh
set -e

CONTAINER="${CONTAINER:-resureccion}"
STOP_SECONDS="${STOP_SECONDS:-60}"

echo "Sleeping ${CONTAINER}: graceful stop (up to ${STOP_SECONDS}s for checkpoints to flush)…"
if docker stop --time "$STOP_SECONDS" "$CONTAINER" >/dev/null 2>&1; then
    echo "Asleep. CPU and RAM released; data + swap preserved on the SSD."
    echo "Wake it with:  sh scripts/host_awake.sh"
else
    echo "Could not stop ${CONTAINER} (is it installed? try: docker ps -a)" >&2
    exit 1
fi
