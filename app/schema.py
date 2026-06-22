from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, create_engine, inspect, text
from sqlalchemy.engine import Engine


REQUIRED_SCHEMA_REVISION = "20260622_0003"


@dataclass(frozen=True)
class SchemaStatus:
    current_revision: str | None
    head_revision: str
    migration_required: bool


@dataclass(frozen=True)
class SchemaValidationReport:
    database_path: str
    database_dialect: str
    detected_schema_type: str
    current_revision: str | None
    target_revision: str
    missing_tables: list[str]
    missing_columns: dict[str, list[str]]
    mismatched_columns: dict[str, list[str]]
    missing_indexes: dict[str, list[str]]
    missing_constraints: dict[str, list[str]]
    missing_foreign_keys: dict[str, list[str]]
    data_preservation_check: dict[str, int]
    recommended_action: str
    safe_to_stamp: bool

    @property
    def migration_required(self) -> bool:
        return self.current_revision != self.target_revision

    def to_dict(self) -> dict[str, Any]:
        return {
            "database_path": self.database_path,
            "database_dialect": self.database_dialect,
            "detected_schema_type": self.detected_schema_type,
            "current_revision": self.current_revision,
            "target_revision": self.target_revision,
            "migration_required": self.migration_required,
            "missing_tables": self.missing_tables,
            "missing_columns": self.missing_columns,
            "mismatched_columns": self.mismatched_columns,
            "missing_indexes": self.missing_indexes,
            "missing_constraints": self.missing_constraints,
            "missing_foreign_keys": self.missing_foreign_keys,
            "data_preservation_check": self.data_preservation_check,
            "recommended_action": self.recommended_action,
            "safe_to_stamp": self.safe_to_stamp,
        }


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config("alembic.ini")
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def schema_status(engine: Engine) -> SchemaStatus:
    head = head_revision()
    current = current_revision(engine)
    return SchemaStatus(
        current_revision=current,
        head_revision=head,
        migration_required=current != head,
    )


def validate_schema_against_metadata(engine: Engine) -> SchemaValidationReport:
    from .db import Base

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    metadata_tables = Base.metadata.tables
    expected_tables = set(metadata_tables)
    missing_tables = sorted(expected_tables - tables)
    missing_columns: dict[str, list[str]] = {}
    mismatched_columns: dict[str, list[str]] = {}
    missing_indexes: dict[str, list[str]] = {}
    missing_constraints: dict[str, list[str]] = {}
    missing_foreign_keys: dict[str, list[str]] = {}
    table_counts = _table_counts(engine, sorted(tables - {"alembic_version"}))

    for table_name, table in metadata_tables.items():
        if table_name not in tables:
            continue
        actual_columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        expected_columns = {column.name: column for column in table.columns}
        missing = sorted(set(expected_columns) - set(actual_columns))
        if missing:
            missing_columns[table_name] = missing
        mismatched = []
        for column_name, expected in expected_columns.items():
            actual = actual_columns.get(column_name)
            if actual is None:
                continue
            if actual["type"]._type_affinity is not expected.type._type_affinity:
                mismatched.append(f"{column_name}: expected {expected.type}, actual {actual['type']}")
        if mismatched:
            mismatched_columns[table_name] = sorted(mismatched)

        actual_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
        expected_indexes = {index.name for index in table.indexes if index.name}
        missing_idx = sorted(expected_indexes - actual_indexes)
        if missing_idx:
            missing_indexes[table_name] = missing_idx

        actual_uniques = {constraint["name"] for constraint in inspector.get_unique_constraints(table_name) if constraint.get("name")}
        expected_uniques = {constraint.name for constraint in table.constraints if isinstance(constraint, UniqueConstraint) and constraint.name}
        missing_unique = sorted(expected_uniques - actual_uniques)
        if missing_unique:
            missing_constraints[table_name] = missing_unique

        actual_fks = {fk.get("name") for fk in inspector.get_foreign_keys(table_name) if fk.get("name")}
        expected_fks = {fk.constraint.name for fk in table.foreign_keys if fk.constraint.name}
        missing_fk = sorted(expected_fks - actual_fks)
        if missing_fk:
            missing_foreign_keys[table_name] = missing_fk

    current = current_revision(engine)
    target = head_revision()
    has_business_tables = bool(tables - {"alembic_version"})
    structural_errors = any([missing_tables, missing_columns, mismatched_columns, missing_indexes, missing_constraints, missing_foreign_keys])
    only_missing_indexes = bool(missing_indexes) and not any([missing_tables, missing_columns, mismatched_columns, missing_constraints, missing_foreign_keys])
    if not has_business_tables:
        detected = "empty"
        action = "upgrade"
        safe_to_stamp = False
    elif current == target and not structural_errors:
        detected = "versioned_head"
        action = "none"
        safe_to_stamp = False
    elif current is not None:
        detected = "known_revision"
        action = "upgrade"
        safe_to_stamp = False
    elif only_missing_indexes:
        detected = "legacy_missing_indexes"
        action = "upgrade"
        safe_to_stamp = False
    elif not structural_errors:
        detected = "init_db_complete_unstamped"
        action = "validated_stamp"
        safe_to_stamp = True
    else:
        detected = "unknown_or_incomplete"
        action = "manual_review"
        safe_to_stamp = False
    return SchemaValidationReport(
        database_path=_database_path(engine),
        database_dialect=engine.dialect.name,
        detected_schema_type=detected,
        current_revision=current,
        target_revision=target,
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        mismatched_columns=mismatched_columns,
        missing_indexes=missing_indexes,
        missing_constraints=missing_constraints,
        missing_foreign_keys=missing_foreign_keys,
        data_preservation_check=table_counts,
        recommended_action=action,
        safe_to_stamp=safe_to_stamp,
    )


def assert_schema_ready_for_writes(engine: Engine) -> None:
    if engine.dialect.name == "sqlite" and engine.url.database in {None, "", ":memory:"}:
        return
    report = validate_schema_against_metadata(engine)
    if report.current_revision != report.target_revision or report.recommended_action not in {"none"}:
        raise RuntimeError(
            "MIGRATION_REQUIRED: database schema is not at required Alembic head; "
            f"current={report.current_revision}, head={report.target_revision}, action={report.recommended_action}"
        )


def adopt_legacy_sqlite(
    database_url: str,
    *,
    dry_run: bool = True,
    backup: bool = False,
    stamp: bool = False,
    upgrade: bool = False,
) -> SchemaValidationReport:
    engine = create_engine(database_url, connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {})
    report = validate_schema_against_metadata(engine)
    if dry_run or not (stamp or upgrade):
        return report
    if not database_url.startswith("sqlite"):
        raise RuntimeError("legacy adoption tool currently supports SQLite only")
    if backup:
        backup_sqlite_database(database_url)
    if stamp:
        if not report.safe_to_stamp:
            raise RuntimeError("refusing to stamp: schema validation did not report safe_to_stamp=true")
        command.stamp(alembic_config(database_url), "head")
        return validate_schema_against_metadata(engine)
    if upgrade:
        if report.detected_schema_type == "legacy_missing_indexes":
            _create_missing_metadata_indexes(engine, report.missing_indexes)
            upgraded = validate_schema_against_metadata(engine)
            if not upgraded.safe_to_stamp:
                raise RuntimeError(f"refusing to stamp after index upgrade: {upgraded.to_dict()}")
            command.stamp(alembic_config(database_url), "head")
            return validate_schema_against_metadata(engine)
        if report.recommended_action != "upgrade":
            raise RuntimeError(f"refusing to upgrade schema type {report.detected_schema_type}; recommended action is {report.recommended_action}")
        command.upgrade(alembic_config(database_url), "head")
        return validate_schema_against_metadata(engine)
    return report


def backup_sqlite_database(database_url: str) -> Path:
    path = Path(database_url.replace("sqlite:///", "", 1))
    if not path.exists():
        return path
    backup_path = path.with_suffix(path.suffix + ".bak")
    counter = 1
    while backup_path.exists():
        backup_path = path.with_suffix(path.suffix + f".bak{counter}")
        counter += 1
    shutil.copy2(path, backup_path)
    return backup_path


def _create_missing_metadata_indexes(engine: Engine, missing_indexes: dict[str, list[str]]) -> None:
    from .db import Base

    with engine.begin() as connection:
        for table_name, index_names in missing_indexes.items():
            table = Base.metadata.tables[table_name]
            indexes = {index.name: index for index in table.indexes if index.name}
            for index_name in index_names:
                index = indexes.get(index_name)
                if index is None:
                    raise RuntimeError(f"missing index {index_name} is not declared in metadata")
                index.create(bind=connection, checkfirst=True)


def validate_baseline_schema(engine: Engine) -> None:
    report = validate_schema_against_metadata(engine)
    if not report.safe_to_stamp and report.recommended_action != "none":
        raise RuntimeError(f"schema baseline validation failed: {report.to_dict()}")


def _table_counts(engine: Engine, table_names: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in table_names:
            try:
                counts[table] = int(connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0)
            except Exception:
                counts[table] = -1
    return counts


def _database_path(engine: Engine) -> str:
    database = engine.url.database
    if database is None:
        return ""
    return str(Path(database).resolve()) if engine.dialect.name == "sqlite" else database
