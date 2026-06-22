#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1}"
curl -fsS "${BASE_URL}/health" >/dev/null
curl -fsS "${BASE_URL}/api/v1/system/status" >/dev/null
curl -fsS "${BASE_URL}/" >/dev/null
