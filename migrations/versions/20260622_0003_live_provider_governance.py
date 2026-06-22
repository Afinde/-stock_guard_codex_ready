"""live provider shadow governance

Revision ID: 20260622_0003
Revises: 20260622_0002
Create Date: 2026-06-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260622_0003"
down_revision = "20260622_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_provider_shadow_runs()
    _create_market_data_admission_results()
    _create_market_data_admission_history()
    _create_market_data_degradation_events()
    _create_market_data_shadow_daily_reports()
    _create_provider_connectivity_tests()


def downgrade() -> None:
    for table in [
        "provider_connectivity_tests",
        "market_data_shadow_daily_reports",
        "market_data_degradation_events",
        "market_data_admission_history",
        "market_data_admission_results",
        "provider_shadow_runs",
    ]:
        if _has_table(table):
            op.drop_table(table)


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return name in inspector.get_table_names()


def _create_provider_shadow_runs() -> None:
    if _has_table("provider_shadow_runs"):
        return
    op.create_table(
        "provider_shadow_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("provider_version", sa.String(80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trading_date", sa.String(10), nullable=False),
        sa.Column("symbol_universe_version", sa.String(80), nullable=False),
        sa.Column("configured_symbol_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("quote_received_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_quote_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_quote_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale_quote_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_quote_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("out_of_order_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("schema_error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("network_error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rate_limit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("availability", sa.String(32), nullable=False, server_default=""),
        sa.Column("average_latency_ms", sa.Float(), nullable=True),
        sa.Column("p50_latency_ms", sa.Float(), nullable=True),
        sa.Column("p95_latency_ms", sa.Float(), nullable=True),
        sa.Column("p99_latency_ms", sa.Float(), nullable=True),
        sa.Column("missing_symbol_rate", sa.String(32), nullable=False, server_default=""),
        sa.Column("account_state_before_checksum", sa.String(80), nullable=False, server_default=""),
        sa.Column("account_state_after_checksum", sa.String(80), nullable=False, server_default=""),
        sa.Column("fills_before_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fills_after_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.String(48), nullable=False),
        sa.Column("failure_reasons_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("run_id", name="uq_provider_shadow_run_id"),
    )
    op.create_index("ix_provider_shadow_runs_provider", "provider_shadow_runs", ["provider"])
    op.create_index("ix_provider_shadow_runs_trading_date", "provider_shadow_runs", ["trading_date"])


def _create_market_data_admission_results() -> None:
    if _has_table("market_data_admission_results"):
        return
    op.create_table(
        "market_data_admission_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("complete_trading_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_reasons_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("policy_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("provider", "evaluated_at", name="uq_market_admission_result"),
    )


def _create_market_data_admission_history() -> None:
    if _has_table("market_data_admission_history"):
        return
    op.create_table(
        "market_data_admission_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )


def _create_market_data_degradation_events() -> None:
    if _has_table("market_data_degradation_events"):
        return
    op.create_table(
        "market_data_degradation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("mode_from", sa.String(32), nullable=False, server_default="LIVE_PAPER"),
        sa.Column("mode_to", sa.String(32), nullable=False, server_default="RECORDED"),
        sa.Column("requires_manual_review", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("event_id", name="uq_market_degradation_event_id"),
    )


def _create_market_data_shadow_daily_reports() -> None:
    if _has_table("market_data_shadow_daily_reports"):
        return
    op.create_table(
        "market_data_shadow_daily_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("trading_date", sa.String(10), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "trading_date", name="uq_market_shadow_daily_report"),
    )


def _create_provider_connectivity_tests() -> None:
    if _has_table("provider_connectivity_tests"):
        return
    op.create_table(
        "provider_connectivity_tests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("test_id", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(48), nullable=False),
        sa.Column("error_type", sa.String(80), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("symbol_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quote_received_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("test_id", name="uq_provider_connectivity_test_id"),
    )
