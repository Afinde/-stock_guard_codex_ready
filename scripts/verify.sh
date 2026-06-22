#!/usr/bin/env bash
set -euo pipefail

if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=.venv/bin/python
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN=python
else
  PYTHON_BIN=python3
fi

"${PYTHON_BIN}" -m compileall -q app tests
"${PYTHON_BIN}" -m pytest -q
