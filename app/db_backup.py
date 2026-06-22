from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import get_settings

TZ = ZoneInfo("Asia/Shanghai")


def sqlite_database_path(database_url: str) -> Path:
    if not database_url.startswith("sqlite:///"):
        raise ValueError("SQLite backup only supports sqlite:/// database URLs")
    return Path(database_url.removeprefix("sqlite:///")).expanduser().resolve()


def backup_sqlite_database(source: Path, backup_dir: Path, *, keep_daily: int = 7, keep_weekly: int = 4) -> Path:
    if not source.exists():
        raise FileNotFoundError(f"database does not exist: {source}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    destination = backup_dir / f"{source.stem}-{timestamp}.sqlite3"
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    with sqlite3.connect(destination) as check:
        check.execute("PRAGMA integrity_check").fetchone()
    _prune_backups(backup_dir, source.stem, keep_daily=keep_daily, keep_weekly=keep_weekly)
    return destination


def _prune_backups(backup_dir: Path, stem: str, *, keep_daily: int, keep_weekly: int) -> None:
    backups = sorted(backup_dir.glob(f"{stem}-*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    keep_count = max(1, keep_daily + keep_weekly)
    for old in backups[keep_count:]:
        old.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup using the SQLite backup API.")
    parser.add_argument("--database", default=None)
    parser.add_argument("--backup-dir", default="backups/sqlite")
    parser.add_argument("--keep-daily", type=int, default=7)
    parser.add_argument("--keep-weekly", type=int, default=4)
    args = parser.parse_args(argv)
    settings = get_settings()
    source = sqlite_database_path(args.database or settings.database_url)
    destination = backup_sqlite_database(source, Path(args.backup_dir).expanduser().resolve(), keep_daily=args.keep_daily, keep_weekly=args.keep_weekly)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
