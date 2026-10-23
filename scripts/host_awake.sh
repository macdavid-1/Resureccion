#!/bin/sh
# Wake Resurrección: start the container after a sleep and wait for health.
# Data and the 2 GB swap file were preserved on the SSD, so the app comes back
# exactly where it left off (research resumes from its last checkpoint).
#
# On the host:  sh scripts/host_awake.sh
set -e

CONTAINER="${CONTAINER:-resureccion}"
PORT="${PORT:-7860}"
TRIES="${TRIES:-30}"

echo "Waking ${CONTAINER}…"
docker start "$CONTAINER" >/dev/null

i=0
while [ "$i" -lt "$TRIES" ]; do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        echo "Awake and healthy at http://127.0.0.1:${PORT}"
        echo "Public URL (once DNS + proxy are set): https://resurreccion.omnivex-academy.xyz"
        exit 0
    fi
    i=$((i + 1))
    sleep 2
done

echo "${CONTAINER} started but /api/health not up after $((TRIES * 2))s — check logs:" >&2
echo "  docker logs --tail 50 ${CONTAINER}" >&2
exit 1
