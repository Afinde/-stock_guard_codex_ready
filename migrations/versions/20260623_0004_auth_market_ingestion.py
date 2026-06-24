"""auth and public market ingestion

Revision ID: 20260623_0004
Revises: 20260622_0003
Create Date: 2026-06-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260623_0004"
down_revision = "20260622_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _users()
    _auth_sessions()
    _login_audit_logs()
    _instruments()
    _daily_bars()
    _stock_news()
    _industry_snapshots()
    _financial_metrics()
    _data_ingestion_runs()
    _provider_health_status()


def downgrade() -> None:
    for table in [
        "provider_health_status",
        "data_ingestion_runs",
        "financial_metrics",
        "industry_snapshots",
        "stock_news",
        "daily_bars",
        "instruments",
        "login_audit_logs",
        "auth_sessions",
        "users",
    ]:
        if _has_table(table):
            op.drop_table(table)


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _users() -> None:
    if _has_table("users"):
        return
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_username", "users", ["username"])
    op.create_index("ix_users_role", "users", ["role"])
    op.create_index("ix_users_is_active", "users", ["is_active"])


def _auth_sessions() -> None:
    if _has_table("auth_sessions"):
        return
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(80), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("refresh_token_hash", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=False, server_default=""),
        sa.Column("ip_address", sa.String(80), nullable=False, server_default=""),
        sa.UniqueConstraint("session_id", name="uq_auth_session_id"),
    )
    op.create_index("ix_auth_sessions_session_id", "auth_sessions", ["session_id"])
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_refresh_token_hash", "auth_sessions", ["refresh_token_hash"])


def _login_audit_logs() -> None:
    if _has_table("login_audit_logs"):
        return
    op.create_table(
        "login_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False, server_default=""),
        sa.Column("ip_address", sa.String(80), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(300), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def _instruments() -> None:
    if _has_table("instruments"):
        return
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False, server_default=""),
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("instrument_type", sa.String(32), nullable=False, server_default="A_SHARE"),
        sa.Column("industry", sa.String(120), nullable=True),
        sa.Column("list_date", sa.String(10), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
        sa.Column("source", sa.String(80), nullable=False, server_default=""),
        sa.Column("source_version", sa.String(80), nullable=False, server_default=""),
        sa.Column("checksum", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", name="uq_instruments_symbol"),
    )


def _daily_bars() -> None:
    if _has_table("daily_bars"):
        return
    op.create_table(
        "daily_bars",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("trading_date", sa.String(10), nullable=False),
        sa.Column("adjust", sa.String(16), nullable=False, server_default=""),
        sa.Column("open_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("high_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("low_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("close_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(24, 4), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("quality_status", sa.String(24), nullable=False, server_default="VALID"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "symbol", "trading_date", "adjust", name="uq_daily_bar_provider_symbol_date_adjust"),
    )


def _stock_news() -> None:
    if _has_table("stock_news"):
        return
    op.create_table(
        "stock_news",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_url_hash", sa.String(64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_url_hash", name="uq_stock_news_source_url_hash"),
        sa.UniqueConstraint("checksum", name="uq_stock_news_checksum"),
    )


def _industry_snapshots() -> None:
    if _has_table("industry_snapshots"):
        return
    op.create_table(
        "industry_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("industry_name", sa.String(120), nullable=False),
        sa.Column("market_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("change_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("turnover", sa.Numeric(24, 4), nullable=True),
        sa.Column("leading_stock", sa.String(16), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("quality_status", sa.String(24), nullable=False, server_default="VALID"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "industry_name", "market_time", "checksum", name="uq_industry_snapshot"),
    )


def _financial_metrics() -> None:
    if _has_table("financial_metrics"):
        return
    op.create_table(
        "financial_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("report_period", sa.String(20), nullable=False),
        sa.Column("metric_name", sa.String(120), nullable=False),
        sa.Column("metric_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("unit", sa.String(32), nullable=False, server_default=""),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "symbol", "report_period", "checksum", name="uq_financial_metric"),
    )


def _data_ingestion_runs() -> None:
    if _has_table("data_ingestion_runs"):
        return
    op.create_table(
        "data_ingestion_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(120), nullable=False),
        sa.Column("job_type", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_summary_json", sa.Text(), nullable=False, server_default="[]"),
        sa.UniqueConstraint("run_id", name="uq_data_ingestion_run_id"),
    )


def _provider_health_status() -> None:
    if _has_table("provider_health_status"):
        return
    op.create_table(
        "provider_health_status",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(80), nullable=False, server_default=""),
        sa.Column("last_error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", name="uq_provider_health_provider"),
    )
