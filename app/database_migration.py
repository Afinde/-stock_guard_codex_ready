from __future__ import annotations

import argparse
import json

from .config import get_settings
from .schema import adopt_legacy_sqlite, alembic_config, backup_sqlite_database, validate_schema_against_metadata
from sqlalchemy import create_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely adopt or migrate the default SQLite database.")
    parser.add_argument("--adopt-legacy", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--stamp", action="store_true")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--database-url")
    args = parser.parse_args(argv)

    settings = get_settings()
    database_url = args.database_url or settings.database_url
    if not any([args.adopt_legacy, args.status, args.validate, args.stamp, args.upgrade]):
        args.dry_run = True
    dry_run = args.dry_run or not (args.stamp or args.upgrade)

    if args.status or args.validate:
        engine = create_engine(database_url, connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {})
        report = validate_schema_against_metadata(engine)
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if args.status or report.safe_to_stamp or report.recommended_action in {"none", "upgrade"} else 1

    if args.backup and dry_run:
        backup_path = backup_sqlite_database(database_url) if database_url.startswith("sqlite") else None
        if backup_path is not None:
            print(json.dumps({"backup_path": str(backup_path)}, ensure_ascii=False, sort_keys=True))

    report = adopt_legacy_sqlite(
        database_url,
        dry_run=dry_run,
        backup=args.backup,
        stamp=args.stamp,
        upgrade=args.upgrade,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
