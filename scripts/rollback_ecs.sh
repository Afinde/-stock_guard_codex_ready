#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.prod.yml}"
echo "Rollback only switches containers/images. Database migrations are not automatically rolled back."
mkdir -p logs/rollback
docker compose -f "${COMPOSE_FILE}" logs --tail=300 > "logs/rollback/$(date +%Y%m%d-%H%M%S).log" || true
docker compose -f "${COMPOSE_FILE}" up -d api web
BASE_URL="${BASE_URL:-http://127.0.0.1}" bash scripts/health_check.sh
