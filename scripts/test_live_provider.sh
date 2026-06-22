#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=.venv/bin/python
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN=python
else
  PYTHON_BIN=python3
fi

SYMBOLS="${MARKET_LIVE_TEST_SYMBOLS:-600000.SH,000001.SZ}"

if [ "${MARKET_LIVE_ENABLED:-false}" != "true" ] || [ -z "${MARKET_LIVE_API_BASE_URL:-}" ]; then
  "$PYTHON_BIN" -m app.market_data_runtime --provider live-paper --shadow --connectivity-test --symbols "$SYMBOLS"
  exit $?
fi

export MARKET_DATA_MODE=LIVE_PAPER
export MARKET_LIVE_PROVIDER="${MARKET_LIVE_PROVIDER:-live_paper}"
export MARKET_LIVE_SHADOW_MODE=true
export MARKET_LIVE_FAIL_CLOSED=true

"$PYTHON_BIN" -m app.market_data_runtime --provider live-paper --shadow --connectivity-test --symbols "$SYMBOLS"
