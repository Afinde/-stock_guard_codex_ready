#!/usr/bin/env bash
set -euo pipefail

python -m app.db_backup "$@"
