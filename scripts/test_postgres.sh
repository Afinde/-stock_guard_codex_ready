#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if command -v python >/dev/null 2>&1; then
  PYTHON_BIN=python
elif [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=.venv/bin/python
else
  PYTHON_BIN=python3
fi

POSTGRES_TEST_PORT="${POSTGRES_TEST_PORT:-55432}"
POSTGRES_TEST_DB="${POSTGRES_TEST_DB:-stock_guard_test}"
POSTGRES_TEST_USER="${POSTGRES_TEST_USER:-stock_guard_test}"
POSTGRES_TEST_PASSWORD="${POSTGRES_TEST_PASSWORD:-stock_guard_test_password}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-stock_guard_pg_test}"
POSTGRES_TEST_PULL_POLICY="${POSTGRES_TEST_PULL_POLICY:-never}"
export POSTGRES_TEST_PORT POSTGRES_TEST_DB POSTGRES_TEST_USER POSTGRES_TEST_PASSWORD COMPOSE_PROJECT_NAME POSTGRES_TEST_PULL_POLICY

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for PostgreSQL integration tests." >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running; start Docker before PostgreSQL integration tests." >&2
  exit 2
fi

COMPOSE=(docker compose -f docker-compose.postgres-test.yml)
"${COMPOSE[@]}" up -d --pull "$POSTGRES_TEST_PULL_POLICY"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "PostgreSQL integration failed; recent container logs:" >&2
    "${COMPOSE[@]}" logs --tail=120 postgres-test >&2 || true
  fi
  "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

echo "Waiting for PostgreSQL test container..."
COMPOSE_FILE=docker-compose.postgres-test.yml \
POSTGRES_TEST_DB="$POSTGRES_TEST_DB" \
POSTGRES_TEST_USER="$POSTGRES_TEST_USER" \
bash scripts/wait_for_postgres.sh

"${COMPOSE[@]}" exec -T postgres-test psql -U "$POSTGRES_TEST_USER" -d "$POSTGRES_TEST_DB" -c "select version();" 

export APP_ENV=test
export DATABASE_URL="postgresql+psycopg://${POSTGRES_TEST_USER}:${POSTGRES_TEST_PASSWORD}@127.0.0.1:${POSTGRES_TEST_PORT}/${POSTGRES_TEST_DB}"
export ENABLE_LIVE_ORDER=false
export MANUAL_CONFIRM_REQUIRED=true
export MARKET_DATA_MODE=FIXTURE
export MARKET_LIVE_ENABLED=false
export MARKET_LIVE_SHADOW_MODE=true
export RUN_POSTGRES_TESTS=1
export PYTHONPATH="$ROOT_DIR"
export PGOPTIONS="${PGOPTIONS:--c lock_timeout=2000 -c statement_timeout=15000}"

"${PYTHON_BIN}" -m pytest -q tests/integration_postgres
