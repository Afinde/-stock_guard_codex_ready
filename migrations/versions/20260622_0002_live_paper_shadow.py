"""live paper shadow market data

Revision ID: 20260622_0002
Revises: 20260620_0001
Create Date: 2026-06-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260622_0002"
down_revision = "20260620_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    _add_column(inspector, "market_quote_snapshots", sa.Column("quality_reasons_json", sa.Text(), server_default="[]"))
    for column in [
        sa.Column("consecutive_successes", sa.Integer(), server_default="0"),
        sa.Column("request_count", sa.Integer(), server_default="0"),
        sa.Column("success_count", sa.Integer(), server_default="0"),
        sa.Column("failure_count", sa.Integer(), server_default="0"),
        sa.Column("p95_latency_ms", sa.Float(), server_default="0"),
        sa.Column("duplicate_quote_count", sa.Integer(), server_default="0"),
        sa.Column("out_of_order_count", sa.Integer(), server_default="0"),
    ]:
        _add_column(inspector, "market_data_provider_status", column)
    for column in [
        sa.Column("provider", sa.String(80), nullable=True),
        sa.Column("market_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_checksum", sa.String(64), nullable=True),
        sa.Column("risk_status", sa.String(32), nullable=True),
        sa.Column("theoretical_quantity", sa.Integer(), nullable=True),
        sa.Column("theoretical_price", sa.String(32), nullable=True),
        sa.Column("theoretical_fees", sa.String(32), nullable=True),
        sa.Column("blocked_reason", sa.Text(), server_default=""),
        sa.Column("account_state_checksum", sa.String(64), nullable=True),
    ]:
        _add_column(inspector, "paper_shadow_decisions", column)
    if "quote_comparisons" not in tables:
        op.create_table(
            "quote_comparisons",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("comparison_id", sa.String(160), nullable=False),
            sa.Column("trading_date", sa.String(10), nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("live_provider", sa.String(80), nullable=False),
            sa.Column("reference_provider", sa.String(80), nullable=False),
            sa.Column("live_quote_id", sa.String(160), nullable=False),
            sa.Column("reference_quote_id", sa.String(160), nullable=False),
            sa.Column("price_diff_bps", sa.String(32), nullable=False),
            sa.Column("latency_ms", sa.Float(), nullable=False, server_default="0"),
            sa.Column("quality_status", sa.String(24), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("comparison_id", name="uq_quote_comparison_id"),
        )
        op.create_index("ix_quote_comparisons_symbol", "quote_comparisons", ["symbol"])
        op.create_index("ix_quote_comparisons_trading_date", "quote_comparisons", ["trading_date"])
    if "market_data_quality_daily" not in tables:
        op.create_table(
            "market_data_quality_daily",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("trading_date", sa.String(10), nullable=False),
            sa.Column("provider", sa.String(80), nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("quote_received_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("valid_quote_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("stale_quote_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("invalid_quote_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duplicate_rate", sa.String(32), nullable=False, server_default=""),
            sa.Column("out_of_order_rate", sa.String(32), nullable=False, server_default=""),
            sa.Column("missing_symbol_rate", sa.String(32), nullable=False, server_default=""),
            sa.Column("average_latency_ms", sa.Float(), nullable=True),
            sa.Column("p50_latency_ms", sa.Float(), nullable=True),
            sa.Column("p95_latency_ms", sa.Float(), nullable=True),
            sa.Column("p99_latency_ms", sa.Float(), nullable=True),
            sa.Column("provider_availability", sa.String(32), nullable=False, server_default=""),
            sa.Column("schema_error_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("price_conflict_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("suspension_unknown_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("limit_rule_unknown_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("trading_date", "provider", "symbol", name="uq_market_quality_daily"),
        )
    if "recorded_quote_files" not in tables:
        op.create_table(
            "recorded_quote_files",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("recording_id", sa.String(160), nullable=False),
            sa.Column("provider", sa.String(80), nullable=False),
            sa.Column("provider_version", sa.String(80), nullable=True),
            sa.Column("request_id", sa.String(160), nullable=False),
            sa.Column("trading_date", sa.String(10), nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("market_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("data_checksum", sa.String(64), nullable=False),
            sa.Column("quality_status", sa.String(24), nullable=False),
            sa.Column("schema_version", sa.String(64), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("recording_id", name="uq_recorded_quote_file_id"),
        )


def downgrade() -> None:
    for table in ["recorded_quote_files", "market_data_quality_daily", "quote_comparisons"]:
        op.drop_table(table)


def _add_column(inspector, table_name: str, column: sa.Column) -> None:
    if table_name not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_columns(table_name)}
    if column.name not in existing:
        op.add_column(table_name, column)
