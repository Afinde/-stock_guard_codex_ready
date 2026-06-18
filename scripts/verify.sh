#!/usr/bin/env bash
set -euo pipefail

python -m compileall -q app tests
python -m pytest -q
