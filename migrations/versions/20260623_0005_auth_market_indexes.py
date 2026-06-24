"""auth and market ingestion indexes

Revision ID: 20260623_0005
Revises: 20260623_0004
Create Date: 2026-06-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260623_0005"
down_revision = "20260623_0004"
branch_labels = None
depends_on = None


INDEXES = {
    "users": ["created_at", "password_changed_at", "updated_at"],
    "auth_sessions": ["expires_at", "issued_at", "revoked_at"],
    "login_audit_logs": ["created_at", "success", "user_id", "username"],
    "instruments": ["checksum", "exchange", "industry", "status", "symbol", "updated_at"],
    "daily_bars": ["adjust", "checksum", "created_at", "provider", "quality_status", "received_at", "symbol", "trading_date"],
    "stock_news": ["checksum", "provider", "published_at", "received_at", "source_url_hash", "symbol"],
    "industry_snapshots": ["checksum", "industry_name", "market_time", "provider", "quality_status", "received_at"],
    "financial_metrics": ["checksum", "metric_name", "provider", "received_at", "report_period", "symbol"],
    "data_ingestion_runs": ["completed_at", "job_type", "provider", "run_id", "started_at", "status"],
    "provider_health_status": ["provider", "status", "updated_at"],
}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table, columns in INDEXES.items():
        if table not in tables:
            continue
        existing = {idx["name"] for idx in inspector.get_indexes(table)}
        for column in columns:
            name = f"ix_{table}_{column}"
            if name not in existing:
                op.create_index(name, table, [column])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table, columns in INDEXES.items():
        if table not in tables:
            continue
        existing = {idx["name"] for idx in inspector.get_indexes(table)}
        for column in reversed(columns):
            name = f"ix_{table}_{column}"
            if name in existing:
                op.drop_index(name, table_name=table)
