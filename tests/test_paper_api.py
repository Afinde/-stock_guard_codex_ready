from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import paper as paper_api
from app.api.paper import router
from app.db import Base, PaperAccountRecord, PaperLedgerEntryRecord, PaperOrderRecord, ProposedOrderRecord
from app.db import (
    MarketDataAdmissionHistoryRecord,
    MarketDataAdmissionResultRecord,
    MarketDataDegradationEventRecord,
    MarketDataProviderStatusRecord,
    MarketDataShadowDailyReportRecord,
    MarketQuoteSnapshotRecord,
    PaperShadowDecisionRecord,
    ProviderConnectivityTestRecord,
    ProviderShadowRunRecord,
    QuoteComparisonRecord,
)
from app.paper import PaperAccountStatus, PaperOrderStatus


TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(paper_api, "SessionLocal", Session)
    monkeypatch.setattr(paper_api, "_now", lambda: datetime(2026, 1, 5, 10, 0, tzinfo=TZ))
    return Session


@pytest.fixture
def client(Session):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def add_account(Session, *, status: str = PaperAccountStatus.ACTIVE.value):
    now = datetime(2026, 1, 5, 9, 0, tzinfo=TZ)
    with Session() as session:
        row = PaperAccountRecord(
            account_id="paper-1",
            name="Fixture",
            status=status,
            initial_cash="100000.00",
            cash_available="100000.00",
            cash_frozen="0.00",
            market_value="0.00",
            total_equity="100000.00",
            realized_pnl="0.00",
            unrealized_pnl="0.00",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()


def add_proposal(Session, *, status: str = "PROPOSED", expired: bool = False):
    now = datetime(2026, 1, 5, 9, 0, tzinfo=TZ)
    with Session() as session:
        row = ProposedOrderRecord(
            proposal_id="proposal-1",
            signal_identity="signal-1",
            risk_decision_id="risk-1",
            symbol="600519",
            side="BUY",
            quantity=500,
            reference_price="10.00",
            stop_price="9.50",
            take_profit_1="10.50",
            take_profit_2="10.80",
            status=status,
            created_at=now,
            expires_at=now - timedelta(minutes=1) if expired else now + timedelta(days=1),
        )
        session.add(row)
        session.commit()


def test_create_account_api_and_list_include_paper_environment(client):
    response = client.post("/api/paper/accounts", json={"account_id": "paper-1", "name": "Demo", "initial_cash": "100000"})
    assert response.status_code == 200
    assert response.json()["environment"] == "PAPER_TRADING"

    listed = client.get("/api/paper/accounts")
    assert listed.status_code == 200
    assert listed.json()["environment"] == "PAPER_TRADING"
    assert listed.json()["data"][0]["paper_trading"] is True


def test_account_detail_positions_orders_fills_ledger_snapshots_query_success(client, Session):
    add_account(Session)

    assert client.get("/api/paper/accounts/paper-1").json()["environment"] == "PAPER_TRADING"
    assert client.get("/api/paper/accounts/paper-1/positions").json()["data"] == []
    assert client.get("/api/paper/accounts/paper-1/orders").json()["data"] == []
    assert client.get("/api/paper/accounts/paper-1/fills").json()["data"] == []
    assert client.get("/api/paper/accounts/paper-1/ledger").json()["data"] == []
    assert client.get("/api/paper/accounts/paper-1/snapshots").json()["data"] == []


def test_missing_resource_returns_404(client):
    response = client.get("/api/paper/accounts/missing")
    assert response.status_code == 404


def test_pause_and_resume_account(client, Session):
    add_account(Session)

    paused = client.post("/api/paper/accounts/paper-1/pause")
    assert paused.status_code == 200
    assert paused.json()["data"]["status"] == PaperAccountStatus.PAUSED.value
    resumed = client.post("/api/paper/accounts/paper-1/resume")
    assert resumed.status_code == 200
    assert resumed.json()["data"]["status"] == PaperAccountStatus.ACTIVE.value


def test_recovery_required_account_cannot_resume(client, Session):
    add_account(Session, status=PaperAccountStatus.PAUSED_RECOVERY_REQUIRED.value)

    response = client.post("/api/paper/accounts/paper-1/resume")
    assert response.status_code == 409


def test_proposal_review_reject_cancel_state_transitions(client, Session):
    add_account(Session)
    add_proposal(Session)

    reviewed = client.post("/api/paper/proposals/proposal-1/review", json={"operator": "tester", "reason": "seen"})
    assert reviewed.status_code == 200
    rejected = client.post("/api/paper/proposals/proposal-1/reject", json={"operator": "tester", "reason": "no"})
    assert rejected.status_code == 200
    illegal = client.post("/api/paper/proposals/proposal-1/cancel", json={"operator": "tester"})
    assert illegal.status_code == 409


def test_accept_proposal_api_creates_unique_paper_order_and_freezes_cash(client, Session):
    add_account(Session)
    add_proposal(Session)

    response = client.post(
        "/api/paper/proposals/proposal-1/accept",
        json={"operator": "tester", "account_id": "paper-1"},
        headers={"Idempotency-Key": "accept-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "PAPER_TRADING"
    assert body["data"]["status"] == PaperOrderStatus.PAPER_PENDING.value
    with Session() as session:
        account = session.scalars(select(PaperAccountRecord)).first()
        assert account.cash_frozen != "0.00"
        assert session.query(PaperOrderRecord).count() == 1
        assert session.query(PaperLedgerEntryRecord).count() == 1


def test_repeated_accept_is_idempotent(client, Session):
    add_account(Session)
    add_proposal(Session)

    first = client.post("/api/paper/proposals/proposal-1/accept", json={"account_id": "paper-1"}, headers={"Idempotency-Key": "same"})
    second = client.post("/api/paper/proposals/proposal-1/accept", json={"account_id": "paper-1"}, headers={"Idempotency-Key": "same"})

    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["paper_order_id"] == second.json()["data"]["paper_order_id"]
    with Session() as session:
        assert session.query(PaperOrderRecord).count() == 1


def test_expired_and_risk_off_proposals_cannot_accept(client, Session):
    add_account(Session)
    add_proposal(Session, expired=True)

    expired = client.post("/api/paper/proposals/proposal-1/accept", json={"account_id": "paper-1"})
    assert expired.status_code == 409

    with Session() as session:
        session.query(ProposedOrderRecord).delete()
        session.query(PaperAccountRecord).delete()
        session.commit()
    add_account(Session, status=PaperAccountStatus.RISK_OFF.value)
    add_proposal(Session)
    risk_off = client.post("/api/paper/proposals/proposal-1/accept", json={"account_id": "paper-1"})
    assert risk_off.status_code == 409


def test_market_data_shadow_read_apis_do_not_modify_accounts(client, Session):
    add_account(Session)
    now = datetime(2026, 1, 5, 10, 0, tzinfo=TZ)
    with Session() as session:
        session.add(
            MarketDataProviderStatusRecord(
                provider="live_paper",
                instance_id="test",
                status="HEALTHY",
                consecutive_failures=0,
                consecutive_successes=2,
                request_count=2,
                success_count=2,
                failure_count=0,
                updated_at=now,
            )
        )
        session.add(
            MarketQuoteSnapshotRecord(
                quote_id="quote-1",
                provider="live_paper",
                provider_version="fake-v1",
                symbol="600519.SH",
                exchange="SSE",
                trading_date="2026-01-05",
                market_time=now,
                received_at=now,
                validated_at=now,
                sequence=None,
                open_price="10.00",
                high_price="10.10",
                low_price="9.90",
                last_price="10.00",
                previous_close="10.00",
                volume=100,
                amount="1000.00",
                bid_price="9.99",
                ask_price="10.00",
                suspension_status="TRADING",
                price_limit_up="11.00",
                price_limit_down="9.00",
                data_checksum="abc",
                calendar_version="cal-v1",
                raw_schema_version="quote-v1",
                quality_status="VALID",
                quality_reasons_json="[]",
                created_at=now,
            )
        )
        session.add(
            PaperShadowDecisionRecord(
                decision_id="shadow-1",
                paper_order_id="order-1",
                account_id="paper-1",
                symbol="600519",
                quote_id="quote-1",
                market_event_id="event-1",
                provider="live_paper",
                market_time=now,
                quote_checksum="abc",
                risk_status="SHADOW_NOT_APPLIED",
                theoretical_quantity=100,
                theoretical_price="10.00",
                theoretical_fees="0.00",
                theoretical_outcome="SHADOW_ELIGIBLE_FOR_MATCHING",
                blocked_reason="",
                account_state_checksum="state",
                created_at=now,
            )
        )
        session.add(
            QuoteComparisonRecord(
                comparison_id="cmp-1",
                trading_date="2026-01-05",
                symbol="600519.SH",
                live_provider="live_paper",
                reference_provider="fixture",
                live_quote_id="quote-1",
                reference_quote_id="quote-2",
                price_diff_bps="0.0000",
                latency_ms=0,
                quality_status="VALID",
                created_at=now,
            )
        )
        session.add(
            ProviderConnectivityTestRecord(
                test_id="conn-1",
                provider="live_paper",
                started_at=now,
                ended_at=now,
                status="PASSED",
                error_type="",
                message="ok",
                symbol_count=1,
                quote_received_count=1,
                payload_json='{"mode":"SHADOW"}',
            )
        )
        session.add(
            ProviderShadowRunRecord(
                run_id="run-1",
                provider="live_paper",
                provider_version="fake-v1",
                started_at=now,
                ended_at=now,
                trading_date="2026-01-05",
                symbol_universe_version="u1",
                configured_symbol_count=1,
                status="COMPLETED",
                quote_received_count=1,
                valid_quote_count=1,
                invalid_quote_count=0,
                stale_quote_count=0,
                duplicate_quote_count=0,
                out_of_order_count=0,
                schema_error_count=0,
                network_error_count=0,
                rate_limit_count=0,
                availability="1.000000",
                average_latency_ms=1,
                p50_latency_ms=1,
                p95_latency_ms=1,
                p99_latency_ms=1,
                missing_symbol_rate="0.000000",
                account_state_before_checksum="same",
                account_state_after_checksum="same",
                fills_before_count=0,
                fills_after_count=0,
                result="PASSED",
                failure_reasons_json="[]",
                payload_json="{}",
            )
        )
        session.add(
            MarketDataAdmissionResultRecord(
                provider="live_paper",
                evaluated_at=now,
                status="OBSERVING",
                complete_trading_days=1,
                failure_reasons_json='["insufficient complete trading days"]',
                metrics_json='{"complete_trading_days":1}',
                policy_snapshot_json='{"minimum_complete_trading_days":10}',
            )
        )
        session.add(
            MarketDataAdmissionHistoryRecord(
                provider="live_paper",
                from_status="NOT_CONFIGURED",
                to_status="OBSERVING",
                reason="first run",
                changed_at=now,
            )
        )
        session.add(
            MarketDataDegradationEventRecord(
                event_id="deg-1",
                provider="live_paper",
                event_type="SCHEMA_CHANGED",
                severity="P0",
                reason="schema",
                mode_from="LIVE_PAPER",
                mode_to="RECORDED",
                requires_manual_review=True,
                created_at=now,
                payload_json='{"mode":"SHADOW"}',
            )
        )
        session.add(
            MarketDataShadowDailyReportRecord(
                provider="live_paper",
                trading_date="2026-01-05",
                status="OBSERVING",
                report_json='{"notice":"not returns","paper_fills_created_count":0}',
                created_at=now,
            )
        )
        before_ledger = session.query(PaperLedgerEntryRecord).count()
        session.commit()

    for path in [
        "/api/paper/market-data/status",
        "/api/paper/market-data/providers",
        "/api/paper/market-data/quotes",
        "/api/paper/market-data/shadow-decisions",
        "/api/paper/market-data/comparisons",
        "/api/paper/market-data/connectivity",
        "/api/paper/market-data/shadow-runs",
        "/api/paper/market-data/shadow-runs/run-1",
        "/api/paper/market-data/admission",
        "/api/paper/market-data/admission/history",
        "/api/paper/market-data/degradation-events",
        "/api/paper/market-data/daily-reports",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert body["environment"] == "PAPER_TRADING"
        assert body["mode"] == "SHADOW"
        assert "API_KEY" not in str(body)

    with Session() as session:
        assert session.query(PaperLedgerEntryRecord).count() == before_ledger
        assert session.scalars(select(PaperAccountRecord)).first().cash_available == "100000.00"


def test_accept_flow_rolls_back_when_cash_insufficient(client, Session):
    add_account(Session)
    add_proposal(Session)
    with Session() as session:
        account = session.scalars(select(PaperAccountRecord)).first()
        account.cash_available = "1.00"
        session.commit()

    response = client.post("/api/paper/proposals/proposal-1/accept", json={"account_id": "paper-1"})

    assert response.status_code == 409
    with Session() as session:
        assert session.query(PaperOrderRecord).count() == 0
        assert session.query(PaperLedgerEntryRecord).count() == 0


def test_cancel_buy_order_releases_frozen_cash(client, Session):
    add_account(Session)
    add_proposal(Session)
    created = client.post("/api/paper/proposals/proposal-1/accept", json={"account_id": "paper-1"}).json()["data"]

    cancelled = client.post(f"/api/paper/orders/{created['paper_order_id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == PaperOrderStatus.CANCELLED.value
    with Session() as session:
        account = session.scalars(select(PaperAccountRecord)).first()
        assert account.cash_frozen == "0.00"


def test_runtime_status_and_tasks_api(client, Session, monkeypatch):
    class FakeRuntime:
        def status(self):
            return {"environment": "PAPER_TRADING", "runtime_enabled": False, "healthy": True}

    monkeypatch.setattr(paper_api, "runtime_from_settings", lambda *_args, **_kwargs: FakeRuntime())
    status = client.get("/api/paper/runtime/status")
    tasks = client.get("/api/paper/runtime/tasks")

    assert status.status_code == 200
    assert status.json()["environment"] == "PAPER_TRADING"
    assert tasks.status_code == 200
    assert tasks.json()["environment"] == "PAPER_TRADING"
