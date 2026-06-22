from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, inspect, text

from app import main as app_main
from app.db import Base
from app.schema import (
    adopt_legacy_sqlite,
    assert_schema_ready_for_writes,
    backup_sqlite_database,
    alembic_config,
    current_revision,
    head_revision,
    validate_baseline_schema,
    validate_schema_against_metadata,
)


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


def test_sqlite_empty_database_upgrade_head_and_repeat_is_idempotent(tmp_path):
    db_path = tmp_path / "migration.db"
    engine = create_engine(sqlite_url(db_path))
    cfg = alembic_config(sqlite_url(db_path))

    command.upgrade(cfg, "head")
    first_revision = current_revision(engine)
    command.upgrade(cfg, "head")

    assert first_revision == head_revision()
    assert current_revision(engine) == head_revision()
    tables = set(inspect(engine).get_table_names())
    assert "paper_orders" in tables
    assert "provider_shadow_runs" in tables
    assert "market_data_admission_results" in tables
    assert "market_data_degradation_events" in tables
    assert "market_data_shadow_daily_reports" in tables
    assert "provider_connectivity_tests" in tables


def test_sqlite_downgrade_then_upgrade_on_temporary_database(tmp_path):
    db_path = tmp_path / "downgrade.db"
    engine = create_engine(sqlite_url(db_path))
    cfg = alembic_config(sqlite_url(db_path))

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert "paper_orders" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")

    assert current_revision(engine) == head_revision()


def test_baseline_stamp_requires_matching_schema(tmp_path):
    db_path = tmp_path / "stamp.db"
    engine = create_engine(sqlite_url(db_path))
    Base.metadata.create_all(bind=engine)
    validate_baseline_schema(engine)
    cfg = alembic_config(sqlite_url(db_path))
    command.stamp(cfg, "head")
    assert current_revision(engine) == head_revision()


def test_baseline_stamp_validation_rejects_mismatched_schema(tmp_path):
    db_path = tmp_path / "bad-stamp.db"
    engine = create_engine(sqlite_url(db_path))
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE paper_orders (id INTEGER PRIMARY KEY)"))
    with pytest.raises(RuntimeError):
        validate_baseline_schema(engine)


def test_empty_database_adoption_recommends_and_performs_upgrade(tmp_path):
    db_path = tmp_path / "empty.db"
    url = sqlite_url(db_path)
    report = adopt_legacy_sqlite(url, dry_run=True)
    assert report.detected_schema_type == "empty"
    assert report.recommended_action == "upgrade"

    upgraded = adopt_legacy_sqlite(url, dry_run=False, backup=True, upgrade=True)
    assert upgraded.current_revision == head_revision()
    assert upgraded.recommended_action == "none"
    assert "paper_orders" in inspect(create_engine(url)).get_table_names()


def test_complete_unstamped_database_requires_validated_stamp(tmp_path):
    db_path = tmp_path / "complete.db"
    url = sqlite_url(db_path)
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    report = validate_schema_against_metadata(engine)
    assert report.detected_schema_type == "init_db_complete_unstamped"
    assert report.safe_to_stamp is True

    stamped = adopt_legacy_sqlite(url, dry_run=False, backup=True, stamp=True)
    assert stamped.current_revision == head_revision()
    assert stamped.migration_required is False


def test_missing_table_and_column_and_type_reject_stamp(tmp_path):
    missing_table_url = sqlite_url(tmp_path / "missing-table.db")
    missing_table_engine = create_engine(missing_table_url)
    with missing_table_engine.begin() as connection:
        connection.execute(text("CREATE TABLE paper_orders (id INTEGER PRIMARY KEY)"))
    missing_table = validate_schema_against_metadata(missing_table_engine)
    assert missing_table.safe_to_stamp is False
    assert missing_table.missing_tables
    with pytest.raises(RuntimeError):
        adopt_legacy_sqlite(missing_table_url, dry_run=False, stamp=True)

    missing_column_url = sqlite_url(tmp_path / "missing-column.db")
    missing_column_engine = create_engine(missing_column_url)
    Base.metadata.create_all(bind=missing_column_engine)
    with missing_column_engine.begin() as connection:
        connection.execute(text("DROP TABLE paper_orders"))
        connection.execute(text("CREATE TABLE paper_orders (id INTEGER PRIMARY KEY)"))
    missing_column = validate_schema_against_metadata(missing_column_engine)
    assert missing_column.safe_to_stamp is False
    assert "paper_orders" in missing_column.missing_columns

    wrong_type_url = sqlite_url(tmp_path / "wrong-type.db")
    wrong_type_engine = create_engine(wrong_type_url)
    Base.metadata.create_all(bind=wrong_type_engine)
    with wrong_type_engine.begin() as connection:
        connection.execute(text("DROP TABLE paper_orders"))
        connection.execute(text("CREATE TABLE paper_orders (id TEXT PRIMARY KEY)"))
    wrong_type = validate_schema_against_metadata(wrong_type_engine)
    assert wrong_type.safe_to_stamp is False
    assert "paper_orders" in wrong_type.mismatched_columns


def test_missing_indexes_upgrade_preserves_data_and_stamps(tmp_path):
    db_path = tmp_path / "missing-index.db"
    url = sqlite_url(db_path)
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_paper_orders_processing_owner"))
        connection.execute(
            text(
                "INSERT INTO paper_orders "
                "(paper_order_id, account_id, active_key, idempotency_key, symbol, side, order_type, quantity, remaining_quantity, status, rejection_reason, source_signal_identity, risk_decision_id, created_at, expires_at, updated_at) "
                "VALUES ('order-1','acct','active','idem','600519','BUY','MARKET',100,100,'PAPER_PENDING','','','','2026-01-01','2026-01-02','2026-01-01')"
            )
        )
    report = validate_schema_against_metadata(engine)
    assert report.detected_schema_type == "legacy_missing_indexes"
    assert report.safe_to_stamp is False

    upgraded = adopt_legacy_sqlite(url, dry_run=False, backup=True, upgrade=True)
    assert upgraded.current_revision == head_revision()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM paper_orders")).scalar() == 1


def test_missing_unique_constraint_rejects_stamp(tmp_path):
    db_path = tmp_path / "missing-unique.db"
    engine = create_engine(sqlite_url(db_path))
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE paper_orders"))
        table = Base.metadata.tables["paper_orders"]
        column_defs = []
        for column in table.columns:
            ddl_type = column.type.compile(dialect=engine.dialect)
            null_sql = "" if column.nullable else " NOT NULL"
            pk_sql = " PRIMARY KEY" if column.primary_key else ""
            column_defs.append(f"{column.name} {ddl_type}{pk_sql}{null_sql}")
        connection.execute(text(f"CREATE TABLE paper_orders ({', '.join(column_defs)})"))
    report = validate_schema_against_metadata(engine)
    assert report.safe_to_stamp is False
    assert "paper_orders" in report.missing_constraints


def test_dry_run_does_not_modify_and_backup_is_created(tmp_path):
    db_path = tmp_path / "dry-run.db"
    url = sqlite_url(db_path)
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    before = current_revision(engine)
    report = adopt_legacy_sqlite(url, dry_run=True, stamp=True)
    assert report.safe_to_stamp is True
    assert current_revision(engine) == before
    backup = backup_sqlite_database(url)
    assert backup.exists()


def test_runtime_guard_blocks_writes_and_health_reports_migration_required(tmp_path, monkeypatch):
    db_path = tmp_path / "guard.db"
    engine = create_engine(sqlite_url(db_path))
    Base.metadata.create_all(bind=engine)

    with pytest.raises(RuntimeError, match="MIGRATION_REQUIRED"):
        assert_schema_ready_for_writes(engine)

    monkeypatch.setattr(app_main, "engine", engine)
    client = TestClient(app_main.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "MIGRATION_REQUIRED"
