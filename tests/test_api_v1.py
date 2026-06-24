from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main as app_main
from app import auth as auth_service
from app.api import auth as auth_api
from app.api import market as market_api
from app.api import paper as paper_api
from app.api import v1
from app.db import (
    BacktestDailyEquityRecord,
    BacktestFillRecord,
    BacktestOrderRecord,
    BacktestRunRecord,
    Base,
    MarketQuoteSnapshotRecord,
    PaperAccountRecord,
    PaperAccountSnapshotRecord,
    PaperFillRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    ScheduledTaskRunRecord,
    SignalRecord,
    UserRecord,
)

TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FakeSchemaReport:
    current_revision: str = "20260623_0005"
    target_revision: str = "20260623_0005"
    migration_required: bool = False
    recommended_action: str = "none"


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(v1, "SessionLocal", Session)
    monkeypatch.setattr(v1, "engine", engine)
    monkeypatch.setattr(v1, "validate_schema_against_metadata", lambda _engine: FakeSchemaReport())
    monkeypatch.setattr(auth_service, "SessionLocal", Session)
    monkeypatch.setattr(auth_api, "SessionLocal", Session)
    monkeypatch.setattr(market_api, "SessionLocal", Session)
    monkeypatch.setattr(market_api, "engine", engine, raising=False)
    v1._dashboard_cache.update({"expires_at": 0.0, "value": None})
    return Session


@pytest.fixture
def client(Session):
    return TestClient(app_main.app)


def seed(Session):
    now = datetime(2026, 1, 5, 15, 1, tzinfo=TZ)
    with Session() as session:
        session.add(
            SignalRecord(
                symbol="600519.SH",
                action="BUY_WATCH",
                signal_type="BUY_WATCH",
                score=82.0,
                price=100.0,
                reference_price=100.0,
                stop_price=95.0,
                stop_loss_price=95.0,
                take_profit_1=105.0,
                take_profit_2=108.0,
                take_profit_1_price=105.0,
                take_profit_2_price=108.0,
                suggested_shares=500,
                reason="trend ok",
                reasons='["收盘价高于MA20和MA60"]',
                invalidation_conditions='["价格跌破止损价"]',
                score_breakdown='{"trend":{"score":30}}',
                strategy_version="1.0.0",
                parameter_version="abc123",
                market_data_source="fixture",
                market_data_checksum="checksum-1",
                market_trade_date="2026-01-05",
                generated_at=now,
            )
        )
        session.add(
            MarketQuoteSnapshotRecord(
                quote_id="quote-1",
                provider="fixture",
                symbol="600519.SH",
                exchange="SH",
                trading_date="2026-01-05",
                market_time=now,
                received_at=now,
                validated_at=now,
                open_price="99.00",
                high_price="101.00",
                low_price="98.00",
                last_price="100.00",
                volume=1000,
                suspension_status="NORMAL",
                data_checksum="bar-checksum",
                calendar_version="cal-v1",
                raw_schema_version="quote-v1",
                quality_status="VALID",
            )
        )
        session.add(
            PaperAccountRecord(
                account_id="paper-1",
                name="Demo",
                status="ACTIVE",
                initial_cash="100000.00",
                cash_available="90000.00",
                cash_frozen="0.00",
                market_value="10000.00",
                total_equity="100000.00",
                realized_pnl="0.00",
                unrealized_pnl="0.00",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            PaperPositionRecord(
                account_id="paper-1",
                symbol="600519.SH",
                total_quantity=100,
                available_quantity=100,
                average_cost="100.00",
                market_value="10000.00",
                unrealized_pnl="0.00",
            )
        )
        session.add(
            PaperOrderRecord(
                paper_order_id="paper-order-1",
                account_id="paper-1",
                idempotency_key="order-key",
                symbol="600519.SH",
                side="BUY",
                order_type="LIMIT",
                quantity=100,
                remaining_quantity=0,
                status="FILLED",
                expires_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            PaperFillRecord(
                fill_id="fill-1",
                paper_order_id="paper-order-1",
                account_id="paper-1",
                symbol="600519.SH",
                side="BUY",
                quantity=100,
                raw_price="100.00",
                execution_price="100.00",
                trade_value="10000.00",
                commission="5.00",
                tax="0.00",
                other_fees="0.00",
                slippage_cost="0.00",
                session_date="2026-01-05",
                filled_at=now,
            )
        )
        session.add(
            PaperAccountSnapshotRecord(
                account_id="paper-1",
                session_date="2026-01-05",
                cash_available="90000.00",
                cash_frozen="0.00",
                market_value="10000.00",
                total_equity="100000.00",
                positions_json="[]",
                created_at=now,
            )
        )
        session.add(
            BacktestRunRecord(
                run_id="bt-1",
                status="SUCCEEDED",
                config_json='{"symbols":["600519.SH"],"start_date":"2026-01-01","end_date":"2026-01-05","initial_cash":"100000"}',
                config_checksum="cfg",
                strategy_name="multi_factor_v1",
                strategy_version="1.0.0",
                parameter_version="abc123",
                calendar_version="cal-v1",
                instrument_rules_version="rules-v1",
                data_checksums_json='{"600519.SH":"bar-checksum"}',
                code_version="test",
                started_at=now,
                result_summary_json='{"final_equity":"101000.00","total_return":"0.01","maximum_drawdown":"0.00","trade_count":1}',
            )
        )
        session.add(BacktestDailyEquityRecord(run_id="bt-1", session_date="2026-01-05", cash="1000.00", market_value="100000.00", total_equity="101000.00", daily_return="0.01", peak_equity="101000.00", drawdown="0.00", exposure="0.99"))
        session.add(BacktestOrderRecord(backtest_order_id="bt-order-1", run_id="bt-1", symbol="600519.SH", side="BUY", order_type="MARKET", quantity=100, remaining_quantity=0, created_session="2026-01-05", earliest_execution_session="2026-01-05", expiry_session="2026-01-05", status="FILLED", source_signal_identity="sig", risk_decision_id="risk"))
        session.add(BacktestFillRecord(fill_id="bt-fill-1", order_id="bt-order-1", symbol="600519.SH", side="BUY", quantity=100, raw_price="100.00", execution_price="100.00", trade_value="10000.00", commission="5.00", tax="0.00", other_fees="0.00", slippage_cost="0.00", session_date="2026-01-05"))
        session.add(ScheduledTaskRunRecord(task_run_id="job-1", task_key="job-1", task_type="WATCHLIST_SCAN", session_date="2026-01-05", status="SUCCEEDED", started_at=now, completed_at=now))
        session.commit()


def login(client, Session, *, role: str = "ADMIN"):
    now = datetime(2026, 1, 5, 9, 0, tzinfo=TZ)
    with Session() as session:
        if session.query(UserRecord).count() == 0:
            session.add(
                UserRecord(
                    username="admin",
                    display_name="Admin",
                    role=role,
                    password_hash=auth_service.hash_password("LongPassword123!"),
                    password_changed_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "LongPassword123!"})
    assert response.status_code == 200
    return response


def test_anonymous_v1_business_api_requires_login(client):
    response = client.get("/api/v1/dashboard/summary")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_dashboard_summary_and_capabilities(client, Session):
    seed(Session)
    login(client, Session)
    response = client.get("/api/v1/dashboard/summary", headers={"X-Request-Id": "rid-1"})
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "rid-1"
    assert body["environment"] == "PAPER_TRADING"
    assert body["data"]["provider_status"] == "NOT_CONFIGURED"
    assert body["data"]["total_signals"] == 1
    assert body["data"]["capabilities"]["live_order"] is False


def test_signals_pagination_filter_and_detail(client, Session):
    seed(Session)
    login(client, Session)
    listed = client.get("/api/v1/signals", params={"page": 1, "page_size": 20, "symbol": "600519.SH", "signal_type": "BUY_WATCH"})
    assert listed.status_code == 200
    page = listed.json()["data"]
    assert page["total"] == 1
    assert page["items"][0]["reasons"] == ["收盘价高于MA20和MA60"]

    detail = client.get(f"/api/v1/signals/{page['items'][0]['signal_id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["strategy_version"] == "1.0.0"


def test_bars_limit_backtests_and_paper_read_only(client, Session):
    seed(Session)
    login(client, Session)
    bars = client.get("/api/v1/stocks/600519.SH/bars", params={"limit": 1200})
    assert bars.status_code == 200
    assert bars.json()["data"]["items"][0]["checksum"] == "bar-checksum"

    backtests = client.get("/api/v1/backtests")
    assert backtests.status_code == 200
    assert backtests.json()["data"]["items"][0]["research_only"] is True
    assert client.get("/api/v1/backtests/bt-1/trades").json()["data"]["total"] == 1

    accounts = client.get("/api/v1/paper/accounts")
    assert accounts.status_code == 200
    assert accounts.json()["data"]["items"][0]["paper_trading"] is True
    assert client.get("/api/v1/paper/accounts/paper-1/positions").json()["data"]["items"][0]["symbol"] == "600519.SH"


def test_system_status_jobs_errors_and_legacy_compatibility(client, Session):
    seed(Session)
    login(client, Session)
    system = client.get("/api/v1/system/status")
    assert system.status_code == 200
    assert "database_path" not in system.text
    assert system.json()["data"]["migration_required"] is False

    jobs = client.get("/api/v1/system/jobs")
    assert jobs.status_code == 200
    assert jobs.json()["data"]["items"][0]["job_id"] == "job-1"

    bad = client.get("/api/v1/signals", params={"sort_by": "1;drop"})
    assert bad.status_code == 422
    assert bad.json()["success"] is False
    assert bad.json()["error"]["code"] == "HTTP_ERROR"
    assert bad.headers["X-Request-Id"]

    legacy = client.get("/api/signals")
    assert legacy.status_code == 200


def test_page_size_cap_and_disabled_backtest_creation(client, Session):
    login(client, Session)
    oversized = client.get("/api/v1/signals", params={"page_size": 101})
    assert oversized.status_code == 422

    create = client.post("/api/v1/backtests")
    assert create.status_code == 403
    assert create.json()["error"]["code"] == "CAPABILITY_DISABLED"


def test_ecs_lite_rejects_legacy_paper_writes(monkeypatch, client):
    monkeypatch.setattr(paper_api, "get_settings", lambda: SimpleNamespace(deployment_profile="ECS_LITE", enable_paper_order_write=False))
    response = client.post("/api/paper/accounts", json={"account_id": "paper-x", "name": "Demo", "initial_cash": "100000"})
    assert response.status_code == 403
