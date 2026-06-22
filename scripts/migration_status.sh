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

echo "Alembic current revision:"
"$PYTHON_BIN" -m alembic current || true
echo "Alembic head revision:"
"$PYTHON_BIN" -m alembic heads
"$PYTHON_BIN" - <<'PY'
from app.db import engine
from app.schema import schema_status, validate_schema_against_metadata

status = schema_status(engine)
print("Schema status:")
print(f"  current={status.current_revision}")
print(f"  head={status.head_revision}")
print(f"  migration_required={status.migration_required}")
report = validate_schema_against_metadata(engine)
print("Schema validation:")
print(f"  database_path={report.database_path}")
print(f"  database_dialect={report.database_dialect}")
print(f"  detected_schema_type={report.detected_schema_type}")
print(f"  recommended_action={report.recommended_action}")
print(f"  safe_to_stamp={report.safe_to_stamp}")
print(f"  missing_tables={report.missing_tables}")
print(f"  missing_columns={report.missing_columns}")
print(f"  mismatched_columns={report.mismatched_columns}")
print(f"  missing_indexes={report.missing_indexes}")
print(f"  missing_constraints={report.missing_constraints}")
PY
