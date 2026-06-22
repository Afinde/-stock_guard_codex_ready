#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.prod.yml}"

test -f .env.prod || { echo ".env.prod is required"; exit 1; }
test -f frontend/dist/index.html || { echo "frontend/dist is required; run frontend build first"; exit 1; }
docker compose -f "${COMPOSE_FILE}" config >/dev/null
bash scripts/backup_sqlite.sh --backup-dir backups/sqlite || true
docker compose -f "${COMPOSE_FILE}" build api
docker compose -f "${COMPOSE_FILE}" run --rm api alembic upgrade head
docker compose -f "${COMPOSE_FILE}" up -d api web
docker compose -f "${COMPOSE_FILE}" ps
BASE_URL="${BASE_URL:-http://127.0.0.1}" bash scripts/health_check.sh
