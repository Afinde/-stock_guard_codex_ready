from __future__ import annotations

import os
import subprocess
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.data_provider import LocalTradingCalendar, MarketDataError
from app.db import (
    Base,
    MarketDataProviderStatusRecord,
    MarketDataQualityDailyRecord,
    MarketQuoteSnapshotRecord,
    MarketDataAdmissionResultRecord,
    MarketDataDegradationEventRecord,
    MarketDataShadowDailyReportRecord,
    NotificationOutboxRecord,
    PaperAccountRecord,
    PaperFillRecord,
    PaperLedgerEntryRecord,
    PaperOrderRecord,
    PaperPositionRecord,
    PaperShadowDecisionRecord,
    ProviderConnectivityTestRecord,
    ProviderShadowRunRecord,
)
from app.paper import PaperAccountStatus, PaperOrderStatus, TestClock
from app.paper_monitor import PaperMarketMonitorService, PaperMonitorConfig
from app.realtime_quotes import (
    FixtureQuoteProvider,
    CompleteShadowTradingDayPolicy,
    LivePaperQuoteProvider,
    MarketDataAdmissionPolicy,
    MarketDataAdmissionService,
    MarketDataDegradationService,
    MarketDataGateway,
    ProviderFieldContract,
    ProviderErrorType,
    ProviderHealthStatus,
    QuoteComparisonService,
    QuoteQualityStatus,
    QuoteProviderError,
    QuoteRecorder,
    QuoteSelectionService,
    RecordedQuoteFileProvider,
    RealTimeQuoteConfig,
    RetryingQuoteProvider,
    SecretRedactor,
    ShadowRunResult,
    admission_status_summary,
    create_shadow_daily_report,
    generate_admission_review_package,
    compute_quality_metrics,
    normalize_quote,
    run_connectivity_check,
    save_quote_snapshot,
)


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=TZ)


@pytest.fixture
def Session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def calendar() -> LocalTradingCalendar:
    return LocalTradingCalendar(
        source="quote-test",
        trading_day_set=frozenset({date(2026, 1, 5)}),
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 5),
        updated_at=datetime(2026, 1, 1, tzinfo=TZ),
        close_time=time(15, 0),
        version="quote-cal-v1",
    )


def raw_quote(**overrides):
    payload = {
        "symbol": "600519",
        "exchange": "SSE",
        "trading_date": "2026-01-05",
        "market_time": NOW.isoformat(),
        "open": "10.00",
        "high": "10.20",
        "low": "9.90",
        "last_price": "10.10",
        "previous_close": "10.00",
        "volume": 10000,
        "amount": "101000.00",
        "bid_price": "10.09",
        "ask_price": "10.10",
        "suspension_status": "TRADING",
        "price_limit_up": "11.00",
        "price_limit_down": "9.00",
    }
    payload.update(overrides)
    return payload


def quote(**overrides):
    now = overrides.pop("now", NOW)
    return normalize_quote(
        raw_quote(**overrides),
        provider=overrides.pop("provider", "fixture"),
        provider_version="fixture-v1",
        received_at=now,
        validated_at=now,
        calendar_version="quote-cal-v1",
        now=now,
        config=RealTimeQuoteConfig(max_age_seconds=300),
    )


def add_account_and_order(session):
    session.add(
        PaperAccountRecord(
            account_id="paper-1",
            name="paper-1",
            status=PaperAccountStatus.ACTIVE.value,
            initial_cash="100000.00",
            cash_available="94850.00",
            cash_frozen="5150.00",
            market_value="0.00",
            total_equity="100000.00",
            realized_pnl="0.00",
            unrealized_pnl="0.00",
            fees_paid_total="0.00",
            taxes_paid_total="0.00",
            peak_equity="100000.00",
            drawdown="0.000000",
            created_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
            updated_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
        )
    )
    session.add(
        PaperOrderRecord(
            paper_order_id="buy-1",
            account_id="paper-1",
            proposal_id="proposal-1",
            active_key="proposal-1",
            idempotency_key="buy-1-idem",
            symbol="600519",
            side="BUY",
            order_type="MARKET_ON_NEXT_OPEN",
            quantity=500,
            remaining_quantity=500,
            status=PaperOrderStatus.PAPER_PENDING.value,
            rejection_reason="",
            source_signal_identity="signal",
            risk_decision_id="risk",
            created_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
            submitted_at=datetime(2026, 1, 5, 9, 1, tzinfo=TZ),
            earliest_execution_at=datetime(2026, 1, 5, 9, 30, tzinfo=TZ),
            expires_at=datetime(2026, 1, 5, 15, 0, tzinfo=TZ),
            updated_at=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
        )
    )


def monitor(Session, *, shadow: bool) -> PaperMarketMonitorService:
    return PaperMarketMonitorService(
        session_factory=Session,
        calendar=calendar(),
        clock=TestClock(NOW),
        config=PaperMonitorConfig(
            enabled=True,
            market_data_mode="LIVE_PAPER",
            shadow_mode=shadow,
            market_data_max_age_seconds=300,
        ),
    )


def test_market_data_mode_defaults_and_invalid_config_fail():
    settings = Settings(_env_file=None)
    assert settings.market_data_mode == "FIXTURE"
    assert settings.market_live_enabled is False
    assert settings.market_live_shadow_mode is True
    with pytest.raises(ValueError):
        Settings(_env_file=None, market_data_mode="BAD")
    with pytest.raises(ValueError):
        Settings(_env_file=None, market_data_mode="LIVE_PAPER", market_live_enabled=False)


def test_quote_normalization_quality_and_checksum():
    first = quote()
    second = quote()
    changed = quote(last_price="10.11")
    negative = quote(last_price="-1")
    bad_ohlc = quote(high="9.00")
    future = quote(market_time=datetime(2026, 1, 5, 10, 1, tzinfo=TZ).isoformat())
    stale = quote(now=datetime(2026, 1, 5, 10, 10, tzinfo=TZ))

    assert first.quality_status == QuoteQualityStatus.VALID.value
    assert first.data_checksum == second.data_checksum
    assert first.data_checksum != changed.data_checksum
    assert negative.quality_status == QuoteQualityStatus.INVALID_PRICE.value
    assert bad_ohlc.quality_status == QuoteQualityStatus.INVALID_PRICE.value
    assert future.quality_status == QuoteQualityStatus.INVALID_TIME.value
    assert stale.quality_status == QuoteQualityStatus.STALE.value


def test_gateway_persists_quotes_and_duplicate_is_idempotent(Session):
    provider = FixtureQuoteProvider([raw_quote()])
    gateway = MarketDataGateway(
        session_factory=Session,
        provider=provider,
        calendar_version="quote-cal-v1",
        clock=TestClock(NOW),
        config=RealTimeQuoteConfig(max_age_seconds=300),
    )
    first = gateway.run_once(["600519"])
    second = gateway.run_once(["600519"])
    with Session() as session:
        rows = session.scalars(select(MarketQuoteSnapshotRecord)).all()
        status = session.scalars(select(MarketDataProviderStatusRecord)).first()
    assert first["saved"] == second["saved"] == 1
    assert len(rows) == 1
    assert status.status == "HEALTHY"


def test_quote_selection_is_deterministic_and_rejects_conflict(Session):
    with Session() as session:
        old = quote(market_time=datetime(2026, 1, 5, 9, 59, tzinfo=TZ).isoformat())
        new = quote()
        save_quote_snapshot(session, old)
        save_quote_snapshot(session, new)
        session.commit()
        selected = QuoteSelectionService(
            session=session,
            clock=TestClock(NOW),
            config=RealTimeQuoteConfig(max_age_seconds=300),
            expected_calendar_version="quote-cal-v1",
        ).select_for_matching("600519", date(2026, 1, 5))
        assert selected.quote_id == new.quote_id

    with Session() as session:
        save_quote_snapshot(session, quote(provider="fixture"))
        save_quote_snapshot(session, quote(provider="recorded", high="12.10", last_price="12.00"))
        session.commit()
        with pytest.raises(MarketDataError):
            QuoteSelectionService(
                session=session,
                clock=TestClock(NOW),
                config=RealTimeQuoteConfig(max_age_seconds=300, provider_conflict_pct=Decimal("0.01")),
                expected_calendar_version="quote-cal-v1",
            ).select_for_matching("600519", date(2026, 1, 5))


def test_shadow_mode_never_modifies_order_fill_account_or_ledger(Session):
    with Session() as session:
        add_account_and_order(session)
        save_quote_snapshot(session, quote())
        session.commit()

    result = monitor(Session, shadow=True).process_order(order_id="buy-1", trading_date=date(2026, 1, 5))

    with Session() as session:
        order = session.scalars(select(PaperOrderRecord)).first()
        account = session.scalars(select(PaperAccountRecord)).first()
        assert result["shadow"] is True
        assert order.status == PaperOrderStatus.PAPER_PENDING.value
        assert account.cash_available == "94850.00"
        assert session.query(PaperFillRecord).count() == 0
        assert session.query(PaperPositionRecord).count() == 0
        assert session.query(PaperLedgerEntryRecord).count() == 0
        decision = session.scalars(select(PaperShadowDecisionRecord)).first()
        assert decision.theoretical_outcome == "SHADOW_THEORETICAL_FILL"
        assert decision.theoretical_quantity == 500
        assert decision.theoretical_price
        assert decision.account_state_checksum
        assert session.scalars(select(NotificationOutboxRecord)).first().notification_type == "SHADOW_MARKET_MONITOR"


def test_shadow_suspension_blocks_theoretical_match_without_mutation(Session):
    with Session() as session:
        add_account_and_order(session)
        save_quote_snapshot(session, quote(suspension_status="SUSPENDED"))
        session.commit()

    result = monitor(Session, shadow=True).process_order(order_id="buy-1", trading_date=date(2026, 1, 5))

    with Session() as session:
        decision = session.scalars(select(PaperShadowDecisionRecord)).first()
        assert result["shadow"] is True
        assert decision.theoretical_outcome == PaperOrderStatus.BLOCKED_SUSPENSION.value
        assert session.query(PaperFillRecord).count() == 0
        assert session.scalars(select(PaperOrderRecord)).first().status == PaperOrderStatus.PAPER_PENDING.value


def test_live_paper_non_shadow_monitor_config_fails(Session):
    with Session() as session:
        add_account_and_order(session)
        save_quote_snapshot(session, quote())
        session.commit()

    with pytest.raises(ValueError, match="non-shadow"):
        monitor(Session, shadow=False)


def test_no_valid_quote_blocks_matching(Session):
    with Session() as session:
        add_account_and_order(session)
        save_quote_snapshot(session, quote(last_price="-1"))
        session.commit()
    with pytest.raises(MarketDataError):
        monitor(Session, shadow=True).process_order(order_id="buy-1", trading_date=date(2026, 1, 5))
    with Session() as session:
        assert session.query(PaperShadowDecisionRecord).count() == 0
        assert session.query(PaperFillRecord).count() == 0


def test_provider_reaches_unavailable_after_consecutive_failures(Session):
    class FailingProvider:
        provider_name = "fixture"
        provider_version = "fail-v1"

        def fetch_quotes(self, symbols, as_of):
            raise ConnectionError("temporary network failure")

        def health_check(self):
            return {}

        def close(self):
            return None

    gateway = MarketDataGateway(
        session_factory=Session,
        provider=FailingProvider(),
        calendar_version="quote-cal-v1",
        clock=TestClock(NOW),
    )
    for _ in range(3):
        with pytest.raises(ConnectionError):
            gateway.run_once(["600519"])
    with Session() as session:
        status = session.scalars(select(MarketDataProviderStatusRecord)).first()
        assert status.status == "UNAVAILABLE"
        assert status.consecutive_failures == 3
        assert session.query(NotificationOutboxRecord).count() >= 1


def test_live_paper_shadow_config_and_fail_closed_defaults():
    settings = Settings(_env_file=None)
    assert settings.market_data_mode == "FIXTURE"
    assert settings.market_live_enabled is False
    assert settings.market_live_shadow_mode is True
    assert settings.market_live_fail_closed is True
    with pytest.raises(ValueError, match="non-shadow"):
        Settings(_env_file=None, market_data_mode="LIVE_PAPER", market_live_enabled=True, market_live_shadow_mode=False)


def test_quote_quality_incomplete_calendar_and_limit_unknown():
    missing_previous = quote(previous_close=None)
    suspension_unknown = quote(suspension_status="UNKNOWN")
    limit_unknown = quote(price_limit_up=None)
    non_trading = normalize_quote(
        raw_quote(trading_date="2026-01-06", market_time=datetime(2026, 1, 6, 10, 0, tzinfo=TZ).isoformat()),
        provider="fixture",
        provider_version="fixture-v1",
        received_at=datetime(2026, 1, 6, 10, 0, tzinfo=TZ),
        validated_at=datetime(2026, 1, 6, 10, 0, tzinfo=TZ),
        calendar_version="quote-cal-v1",
        now=datetime(2026, 1, 6, 10, 0, tzinfo=TZ),
        config=RealTimeQuoteConfig(max_age_seconds=300),
        trading_calendar=calendar(),
    )

    assert missing_previous.quality_status == QuoteQualityStatus.INCOMPLETE.value
    assert suspension_unknown.quality_status == QuoteQualityStatus.SUSPENSION_UNKNOWN.value
    assert limit_unknown.quality_status == QuoteQualityStatus.LIMIT_RULE_UNKNOWN.value
    assert non_trading.quality_status == QuoteQualityStatus.CALENDAR_MISMATCH.value
    assert non_trading.quality_reasons


def test_retrying_provider_retries_only_retryable_errors():
    calls = {"count": 0}
    sleeps: list[float] = []

    class TemporaryProvider(FixtureQuoteProvider):
        provider_name = "live_paper"

        def fetch_quotes(self, symbols, as_of):
            calls["count"] += 1
            if calls["count"] == 1:
                raise QuoteProviderError("timeout", error_type=ProviderErrorType.READ_TIMEOUT)
            return [raw_quote()]

    provider = RetryingQuoteProvider(
        TemporaryProvider(),
        max_attempts=3,
        initial_backoff_seconds=1,
        max_backoff_seconds=2,
        jitter_seconds=0,
        sleep=sleeps.append,
        random_fn=lambda: 0,
    )
    assert provider.fetch_quotes(["600519"], NOW)
    assert calls["count"] == 2
    assert sleeps == [1]

    class AuthProvider(FixtureQuoteProvider):
        def fetch_quotes(self, symbols, as_of):
            raise QuoteProviderError("auth", error_type=ProviderErrorType.AUTHENTICATION_ERROR)

    with pytest.raises(QuoteProviderError):
        RetryingQuoteProvider(AuthProvider(), sleep=sleeps.append).fetch_quotes(["600519"], NOW)


def test_live_paper_provider_fake_fetcher_and_disabled_boundary():
    provider = LivePaperQuoteProvider(fetcher=lambda symbols, as_of: [raw_quote(symbol=symbols[0])])
    assert provider.fetch_quotes(["600519"], NOW)[0]["symbol"] == "600519"
    disabled = LivePaperQuoteProvider()
    assert disabled.health_check()["status"] == ProviderHealthStatus.NOT_CONFIGURED.value
    with pytest.raises(QuoteProviderError) as exc:
        disabled.fetch_quotes(["600519"], NOW)
    assert exc.value.error_type == ProviderErrorType.PROVIDER_DISABLED


def test_provider_field_contract_and_secret_redactor():
    contract = ProviderFieldContract(provider_symbol="code", last_price="price", market_time="ts")
    mapped = contract.map_quote({**raw_quote(), "code": "600519.SH", "price": "10.20", "ts": NOW.isoformat()})
    assert mapped["symbol"] == "600519.SH"
    assert mapped["last_price"] == "10.20"

    redactor = SecretRedactor(["secret-token", "acct-1"])
    text = redactor.redact_text("Authorization: secret-token account=acct-1")
    mapping = redactor.redact_mapping({"api_key": "secret-token", "nested": {"password": "secret-token"}, "safe": "acct-1"})
    assert "secret-token" not in text
    assert "acct-1" not in text
    assert mapping["api_key"] == "***REDACTED***"
    assert mapping["nested"]["password"] == "***REDACTED***"
    assert mapping["safe"] == "***REDACTED***"


def test_quote_recording_and_replay_are_deterministic(Session, tmp_path):
    snap = quote()
    with Session() as session:
        recorder = QuoteRecorder(tmp_path)
        path = recorder.record(session, request_id="req-1", snapshot=snap)
        session.commit()
    assert path.exists()

    provider = RecordedQuoteFileProvider(tmp_path)
    replayed = provider.fetch_quotes(["600519.SH"], NOW)
    replayed_snap = normalize_quote(
        replayed[0],
        provider="recorded",
        provider_version="recorded-file-v1",
        received_at=NOW,
        validated_at=NOW,
        calendar_version="quote-cal-v1",
        now=NOW,
        config=RealTimeQuoteConfig(max_age_seconds=300),
    )
    assert replayed_snap.data_checksum == snap.data_checksum
    with Session() as session:
        assert session.query(MarketDataQualityDailyRecord).count() == 0


def test_quote_comparison_and_quality_metrics(Session):
    live = quote(provider="live_paper", last_price="10.30")
    ref = quote(provider="fixture", last_price="10.00")
    service = QuoteComparisonService(conflict_bps=100)
    result = service.compare(live, ref, created_at=NOW)
    assert result["quality_status"] == QuoteQualityStatus.PRICE_CONFLICT.value
    metrics = compute_quality_metrics([live, ref], expected_symbols={"600519.SH", "000001.SZ"})
    assert metrics["quote_received_count"] == 2
    assert metrics["missing_symbol_rate"] != "0.000000"
    with Session() as session:
        row = service.save(session, result)
        session.commit()
        assert row.quality_status == QuoteQualityStatus.PRICE_CONFLICT.value


def test_provider_recovers_after_configured_successes(Session):
    class FlappingProvider(FixtureQuoteProvider):
        provider_name = "live_paper"

        def __init__(self):
            super().__init__([raw_quote()])
            self.calls = 0

        def fetch_quotes(self, symbols, as_of):
            self.calls += 1
            if self.calls <= 2:
                raise QuoteProviderError("server", error_type=ProviderErrorType.TEMPORARY_SERVER_ERROR)
            return [raw_quote()]

    gateway = MarketDataGateway(
        session_factory=Session,
        provider=FlappingProvider(),
        calendar_version="quote-cal-v1",
        clock=TestClock(NOW),
        config=RealTimeQuoteConfig(max_age_seconds=300, provider_failure_threshold=2, provider_recovery_success_count=2),
    )
    for _ in range(2):
        with pytest.raises(QuoteProviderError):
            gateway.run_once(["600519"])
    gateway.run_once(["600519"])
    gateway.run_once(["600519"])
    with Session() as session:
        status = session.scalars(select(MarketDataProviderStatusRecord)).first()
        assert status.status == "HEALTHY"
        assert status.failure_count == 2
        assert status.success_count == 2


def test_provider_error_classification_for_http_statuses():
    class ResponseClient:
        def __init__(self, status_code, *, headers=None, payload=None):
            self.status_code = status_code
            self.headers = headers or {}
            self.payload = payload or {}

        def get(self, *args, **kwargs):
            return self

        def json(self):
            return self.payload

        def close(self):
            return None

    cases = [
        (401, {}, ProviderErrorType.AUTHENTICATION_ERROR),
        (403, {}, ProviderErrorType.PERMISSION_DENIED),
        (429, {}, ProviderErrorType.RATE_LIMITED),
        (429, {"X-Quota-Exceeded": "true"}, ProviderErrorType.QUOTA_EXCEEDED),
        (404, {}, ProviderErrorType.SYMBOL_NOT_FOUND),
        (503, {}, ProviderErrorType.PROVIDER_MAINTENANCE),
        (502, {}, ProviderErrorType.TEMPORARY_SERVER_ERROR),
    ]
    for status_code, headers, expected in cases:
        provider = LivePaperQuoteProvider(api_base_url="https://example.invalid", client=ResponseClient(status_code, headers=headers))
        with pytest.raises(QuoteProviderError) as exc:
            provider.fetch_quotes(["600519"], NOW)
        assert exc.value.error_type == expected

    provider = LivePaperQuoteProvider(api_base_url="https://example.invalid", client=ResponseClient(200, payload={"bad": []}))
    with pytest.raises(QuoteProviderError) as exc:
        provider.fetch_quotes(["600519"], NOW)
    assert exc.value.error_type == ProviderErrorType.SCHEMA_CHANGED


def test_retrying_provider_retries_rate_limited_but_not_schema_changed():
    sleeps: list[float] = []

    class RateLimitedThenOk(FixtureQuoteProvider):
        provider_name = "live_paper"

        def __init__(self):
            super().__init__()
            self.calls = 0

        def fetch_quotes(self, symbols, as_of):
            self.calls += 1
            if self.calls == 1:
                raise QuoteProviderError("slow down", error_type=ProviderErrorType.RATE_LIMITED)
            return [raw_quote()]

    rate_provider = RateLimitedThenOk()
    assert RetryingQuoteProvider(rate_provider, sleep=sleeps.append, jitter_seconds=0).fetch_quotes(["600519"], NOW)
    assert rate_provider.calls == 2
    assert sleeps == [1]

    class SchemaChanged(FixtureQuoteProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def fetch_quotes(self, symbols, as_of):
            self.calls += 1
            raise QuoteProviderError("schema", error_type=ProviderErrorType.SCHEMA_CHANGED)

    schema_provider = SchemaChanged()
    with pytest.raises(QuoteProviderError):
        RetryingQuoteProvider(schema_provider, sleep=sleeps.append).fetch_quotes(["600519"], NOW)
    assert schema_provider.calls == 1


def test_gateway_records_provider_shadow_run_without_paper_mutation(Session):
    with Session() as session:
        add_account_and_order(session)
        session.commit()
    gateway = MarketDataGateway(
        session_factory=Session,
        provider=FixtureQuoteProvider([raw_quote()]),
        calendar_version="quote-cal-v1",
        clock=TestClock(NOW),
        config=RealTimeQuoteConfig(max_age_seconds=300),
    )
    gateway.run_once(["600519"])
    with Session() as session:
        run = session.scalars(select(ProviderShadowRunRecord)).first()
        assert run.result == ShadowRunResult.PASSED.value
        assert run.account_state_before_checksum == run.account_state_after_checksum
        assert run.fills_before_count == run.fills_after_count == 0
        assert session.query(PaperFillRecord).count() == 0
        assert session.scalars(select(PaperOrderRecord)).first().status == PaperOrderStatus.PAPER_PENDING.value


def test_gateway_provider_failure_records_shadow_run_and_status(Session):
    class DisabledProvider(FixtureQuoteProvider):
        provider_name = "live_paper"
        provider_version = "disabled-v1"

        def fetch_quotes(self, symbols, as_of):
            raise QuoteProviderError("not configured", error_type=ProviderErrorType.PROVIDER_DISABLED)

    gateway = MarketDataGateway(session_factory=Session, provider=DisabledProvider(), calendar_version="quote-cal-v1", clock=TestClock(NOW))
    with pytest.raises(QuoteProviderError):
        gateway.run_once(["600519"])
    with Session() as session:
        run = session.scalars(select(ProviderShadowRunRecord)).first()
        status = session.scalars(select(MarketDataProviderStatusRecord)).first()
        assert run.result == ShadowRunResult.PROVIDER_NOT_CONFIGURED.value
        assert "PROVIDER_DISABLED" in run.failure_reasons_json
        assert status.status == ProviderHealthStatus.NOT_CONFIGURED.value


def _add_shadow_run(
    session,
    *,
    trading_date: str,
    provider: str = "live_paper",
    availability: str = "1.000000",
    invalid: int = 0,
    result: str = ShadowRunResult.PASSED.value,
    suffix: str = "",
    hour: int = 10,
):
    session.add(
        ProviderShadowRunRecord(
            run_id=f"run-{provider}-{trading_date}{suffix}",
            provider=provider,
            provider_version="test-v1",
            started_at=datetime.fromisoformat(f"{trading_date}T{hour:02d}:00:00+08:00"),
            ended_at=datetime.fromisoformat(f"{trading_date}T{hour:02d}:01:00+08:00"),
            trading_date=trading_date,
            symbol_universe_version="u1",
            configured_symbol_count=1,
            status="COMPLETED",
            quote_received_count=100,
            valid_quote_count=100 - invalid,
            invalid_quote_count=invalid,
            stale_quote_count=0,
            duplicate_quote_count=0,
            out_of_order_count=0,
            schema_error_count=0,
            network_error_count=0,
            rate_limit_count=0,
            availability=availability,
            average_latency_ms=10,
            p50_latency_ms=10,
            p95_latency_ms=20,
            p99_latency_ms=30,
            missing_symbol_rate="0.000000",
            account_state_before_checksum="same",
            account_state_after_checksum="same",
            fills_before_count=0,
            fills_after_count=0,
            result=result,
            failure_reasons_json="[]",
            payload_json="{}",
        )
    )


def test_market_data_admission_statuses(Session):
    service = MarketDataAdmissionService(MarketDataAdmissionPolicy(minimum_complete_trading_days=2))
    with Session() as session:
        result = service.evaluate(session, provider="live_paper", now=NOW)
        assert result.status == "NOT_CONFIGURED"
        session.rollback()

        session.add(MarketDataProviderStatusRecord(provider="live_paper", instance_id="i1", status="HEALTHY", updated_at=NOW))
        _add_shadow_run(session, trading_date="2026-01-05")
        session.flush()
        observing = service.evaluate(session, provider="live_paper", now=NOW)
        assert observing.status == "OBSERVING"
        session.rollback()

        session.add(MarketDataProviderStatusRecord(provider="live_paper", instance_id="i1", status="HEALTHY", updated_at=NOW))
        _add_shadow_run(session, trading_date="2026-01-05")
        _add_shadow_run(session, trading_date="2026-01-06", invalid=50)
        session.flush()
        bad = service.evaluate(session, provider="live_paper", now=NOW)
        assert bad.status == "INELIGIBLE"
        session.rollback()

        session.add(MarketDataProviderStatusRecord(provider="live_paper", instance_id="i1", status="HEALTHY", updated_at=NOW))
        _add_shadow_run(session, trading_date="2026-01-05")
        _add_shadow_run(session, trading_date="2026-01-06")
        session.flush()
        good = service.evaluate(session, provider="live_paper", now=NOW)
        assert good.status == "ELIGIBLE_FOR_REVIEW"


def test_degradation_event_and_daily_report(Session):
    with Session() as session:
        event = MarketDataDegradationService().evaluate_and_record(
            session,
            provider="live_paper",
            now=NOW,
            status=ProviderHealthStatus.SCHEMA_CHANGED.value,
            reason="field missing",
        )
        assert event is not None
        assert event.severity == "P0"
        assert session.query(NotificationOutboxRecord).count() == 1
        _add_shadow_run(session, trading_date="2026-01-05")
        report = create_shadow_daily_report(session, provider="live_paper", trading_date=date(2026, 1, 5), now=NOW, admission_status="OBSERVING")
        payload = report.report_json
        assert "不代表真实成交" in payload
        assert "paper_fills_created_count" in payload
        session.commit()
    with Session() as session:
        assert session.query(MarketDataDegradationEventRecord).count() == 1
        assert session.query(MarketDataShadowDailyReportRecord).count() == 1
        assert session.query(MarketDataAdmissionResultRecord).count() == 0


def test_unconfigured_connectivity_check_returns_not_configured(Session):
    settings = Settings(_env_file=None)
    summary, exit_code = run_connectivity_check(
        session_factory=Session,
        provider=LivePaperQuoteProvider(),
        settings=settings,
        calendar_version="quote-cal-v1",
        clock=TestClock(NOW),
        symbols=["600519.SH"],
    )
    assert exit_code != 0
    assert summary["status"] == "PROVIDER_NOT_CONFIGURED"
    assert summary["provider_configured"] is False
    assert summary["quotes_received"] == 0
    with Session() as session:
        row = session.scalars(select(ProviderConnectivityTestRecord)).first()
        assert row.status == "PROVIDER_NOT_CONFIGURED"


def test_unconfigured_live_provider_script_returns_nonzero(tmp_path):
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{tmp_path / 'live-provider.db'}",
        "MARKET_LIVE_ENABLED": "false",
        "MARKET_LIVE_API_BASE_URL": "",
        "PYTHONPATH": ".",
    }
    result = subprocess.run(["bash", "scripts/test_live_provider.sh"], cwd=os.getcwd(), env=env, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "PROVIDER_NOT_CONFIGURED" in result.stdout
    assert "environment=PAPER_TRADING" in result.stdout
    assert "fills_created=0" in result.stdout


def test_complete_shadow_day_policy_requires_sessions_and_replay(Session):
    with Session() as session:
        _add_shadow_run(session, trading_date="2026-01-05", suffix="-am", hour=10)
        session.flush()
        runs = session.scalars(select(ProviderShadowRunRecord)).all()
        morning_only = CompleteShadowTradingDayPolicy().evaluate(trading_calendar=calendar(), trading_date=date(2026, 1, 5), runs=runs)
        assert morning_only["day_status"] == "INCOMPLETE"
        assert morning_only["morning_session_complete"] is True
        assert morning_only["afternoon_session_complete"] is False

        _add_shadow_run(session, trading_date="2026-01-05", suffix="-pm", hour=14)
        session.flush()
        runs = session.scalars(select(ProviderShadowRunRecord)).all()
        complete = CompleteShadowTradingDayPolicy().evaluate(trading_calendar=calendar(), trading_date=date(2026, 1, 5), runs=runs)
        assert complete["day_status"] == "COMPLETE"

        replay_failed = CompleteShadowTradingDayPolicy().evaluate(
            trading_calendar=calendar(),
            trading_date=date(2026, 1, 5),
            runs=runs,
            replay_consistency=Decimal("0.99"),
        )
        assert replay_failed["day_status"] == "INCOMPLETE"


def test_shadow_daily_report_contains_required_fields_and_mutation_failures(Session):
    with Session() as session:
        _add_shadow_run(session, trading_date="2026-01-05", suffix="-am", hour=10)
        _add_shadow_run(session, trading_date="2026-01-05", suffix="-pm", hour=14)
        report = create_shadow_daily_report(
            session,
            provider="live_paper",
            trading_date=date(2026, 1, 5),
            now=NOW,
            admission_status="OBSERVING",
            trading_calendar=calendar(),
        )
        payload = report.report_json
        assert "在线行情质量和Shadow决策验证" in payload
        body = __import__("json").loads(payload)
        assert body["day_status"] == "COMPLETE"
        assert body["morning_session_complete"] is True
        assert body["afternoon_session_complete"] is True
        assert body["fills_created"] == 0
        assert "账户收益" in body["notice"]


def test_admission_progress_counts_only_complete_live_paper_days(Session):
    with Session() as session:
        for provider in ["fixture", "recorded"]:
            session.add(
                MarketDataShadowDailyReportRecord(
                    provider=provider,
                    trading_date="2026-01-05",
                    status="COMPLETE",
                    report_json='{"day_status":"COMPLETE","provider_availability":"1","symbol_coverage":"1","p95_latency_ms":"1","missing_symbol_rate":"0","invalid_quote_rate":"0","schema_error_count":0,"fills_created":0,"account_immutability":"1"}',
                    created_at=NOW,
                )
            )
        session.flush()
        fixture_progress = admission_status_summary(
            session,
            provider="fixture",
            policy=MarketDataAdmissionPolicy(minimum_complete_trading_days=1),
            provider_configured=True,
        )
        assert fixture_progress["completed_qualified_days"] == 0
        assert "fixture or recorded" in ";".join(fixture_progress["current_blockers"])

        session.add(MarketDataProviderStatusRecord(provider="live_paper", instance_id="i1", status="HEALTHY", updated_at=NOW))
        session.add(
            MarketDataShadowDailyReportRecord(
                provider="live_paper",
                trading_date="2026-01-05",
                status="INCOMPLETE",
                report_json='{"day_status":"INCOMPLETE"}',
                created_at=NOW,
            )
        )
        observing = admission_status_summary(
            session,
            provider="live_paper",
            policy=MarketDataAdmissionPolicy(minimum_complete_trading_days=2),
            provider_configured=True,
        )
        assert observing["admission_status"] == "OBSERVING"
        assert observing["completed_qualified_days"] == 0

        for day in ["2026-01-06", "2026-01-07"]:
            session.add(
                MarketDataShadowDailyReportRecord(
                    provider="live_paper",
                    trading_date=day,
                    status="COMPLETE",
                    report_json='{"day_status":"COMPLETE","provider_availability":"1","symbol_coverage":"1","p95_latency_ms":"1","missing_symbol_rate":"0","invalid_quote_rate":"0","schema_error_count":0,"fills_created":0,"account_immutability":"1"}',
                    created_at=NOW,
                )
            )
        session.flush()
        eligible = admission_status_summary(
            session,
            provider="live_paper",
            policy=MarketDataAdmissionPolicy(minimum_complete_trading_days=2),
            provider_configured=True,
        )
        assert eligible["admission_status"] == "ELIGIBLE_FOR_REVIEW"


def test_admission_progress_10_days_metric_failure_is_ineligible(Session):
    with Session() as session:
        for index in range(10):
            session.add(
                MarketDataShadowDailyReportRecord(
                    provider="live_paper",
                    trading_date=f"2026-01-{index + 1:02d}",
                    status="COMPLETE",
                    report_json='{"day_status":"COMPLETE","provider_availability":"0.5","symbol_coverage":"1","p95_latency_ms":"1","missing_symbol_rate":"0","invalid_quote_rate":"0","schema_error_count":0,"fills_created":0,"account_immutability":"1"}',
                    created_at=NOW,
                )
            )
        progress = admission_status_summary(
            session,
            provider="live_paper",
            policy=MarketDataAdmissionPolicy(minimum_complete_trading_days=10),
            provider_configured=True,
        )
        assert progress["admission_status"] == "INELIGIBLE"
        assert "provider_availability" in progress["failed_metrics"]


def test_admission_review_package_requires_human_conclusion_and_excludes_secrets(Session, tmp_path):
    with Session() as session:
        session.add(
            MarketDataShadowDailyReportRecord(
                provider="live_paper",
                trading_date="2026-01-05",
                status="INCOMPLETE",
                report_json='{"day_status":"INCOMPLETE","failure_reasons":["missing afternoon"]}',
                created_at=NOW,
            )
        )
        paths = generate_admission_review_package(
            session,
            provider="live_paper",
            policy=MarketDataAdmissionPolicy(minimum_complete_trading_days=10),
            provider_configured=True,
            output_dir=tmp_path,
            now=NOW,
        )
    json_text = paths["json"].read_text(encoding="utf-8")
    md_text = paths["markdown"].read_text(encoding="utf-8")
    assert "TO_BE_FILLED_BY_HUMAN_REVIEWER" in json_text
    assert "APPROVED_FOR_LIMITED_PAPER_REVIEW" in json_text
    assert '"human_review_conclusion":"APPROVED' not in json_text
    assert "secret-token" not in json_text
    assert "账户收益" in md_text
