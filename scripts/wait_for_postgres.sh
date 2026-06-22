#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.postgres-test.yml}"
SERVICE_NAME="${POSTGRES_TEST_SERVICE:-postgres-test}"
DB_NAME="${POSTGRES_TEST_DB:-stock_guard_test}"
DB_USER="${POSTGRES_TEST_USER:-stock_guard_test}"
TIMEOUT_SECONDS="${POSTGRES_TEST_WAIT_SECONDS:-60}"

COMPOSE=(docker compose -f "$COMPOSE_FILE")

for _ in $(seq 1 "$TIMEOUT_SECONDS"); do
  container_id="$("${COMPOSE[@]}" ps -q "$SERVICE_NAME" 2>/dev/null || true)"
  health=""
  if [ -n "$container_id" ]; then
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
  fi
  if [ "$health" = "healthy" ] && "${COMPOSE[@]}" exec -T "$SERVICE_NAME" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done

echo "PostgreSQL test container did not become healthy within ${TIMEOUT_SECONDS}s." >&2
"${COMPOSE[@]}" ps >&2 || true
"${COMPOSE[@]}" logs --tail=100 "$SERVICE_NAME" >&2 || true
exit 1
