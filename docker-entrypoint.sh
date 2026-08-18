#!/bin/sh
set -e

# app/ is bind-mounted from the host (docker-compose.yml: volumes: ./app:/app),
# so file ownership at container start is whatever the host filesystem has —
# any `chown` baked into the image at build time is irrelevant, the mount
# overlays it. Fix ownership here, at runtime, scoped to just DATA_DIR (the
# only path the app writes to: sessions, tokens, rate-limit files, survey
# output — see services/storage.py, services/mcp_client.py, services/rate_limiter.py).
DATA_DIR="${DATA_DIR:-data}"
case "$DATA_DIR" in
    /*) ;;
    *) DATA_DIR="/app/$DATA_DIR" ;;
esac
mkdir -p "$DATA_DIR"
chown -R appuser:appuser "$DATA_DIR"

exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
