from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth as auth_service
from app import data_jobs
from app import main as app_main
from app.api import auth as auth_api
from app.api import market as market_api
from app.api import v1
from app.db import AuthSessionRecord, Base, DataIngestionRunRecord, LoginAuditLogRecord, MarketQuoteSnapshotRecord, SessionLocal, UserRecord
from app.public_market_data import FixtureProvider, normalize_eastmoney_spot

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    for module in [auth_service, auth_api, market_api, v1, data_jobs]:
        monkeypatch.setattr(module, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(data_jobs, "engine", engine)
    return Session


@pytest.fixture
def client(Session):
    return TestClient(app_main.app)


def add_user(Session, *, username: str = "admin", role: str = "ADMIN", active: bool = True):
    now = datetime(2026, 1, 5, 9, 0, tzinfo=TZ)
    with Session() as session:
        row = UserRecord(username=username, display_name=username, role=role, password_hash=auth_service.hash_password("LongPassword123!"), is_active=active, password_changed_at=now, created_at=now, updated_at=now)
        session.add(row)
        session.commit()
        return row.id


def login(client, Session, *, username: str = "admin", password: str = "LongPassword123!", role: str = "ADMIN"):
    if not _has_user(Session, username):
        add_user(Session, username=username, role=role)
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def _has_user(Session, username: str) -> bool:
    with Session() as session:
        return session.scalars(select(UserRecord).where(UserRecord.username == username)).first() is not None


def test_login_success_error_password_and_unknown_user_audit(client, Session):
    add_user(Session)
    ok = client.post("/api/v1/auth/login", json={"username": "admin", "password": "LongPassword123!"})
    assert ok.status_code == 200
    assert "sg_access_token" in ok.cookies
    wrong = client.post("/api/v1/auth/login", json={"username": "admin", "password": "bad"})
    missing = client.post("/api/v1/auth/login", json={"username": "missing", "password": "bad"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["error"]["message"] == missing.json()["error"]["message"]
    with Session() as session:
        assert session.query(LoginAuditLogRecord).count() == 3


def test_login_lockout_refresh_logout_and_disabled_user(client, Session):
    user_id = add_user(Session)
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "bad"})
    locked = client.post("/api/v1/auth/login", json={"username": "admin", "password": "LongPassword123!"})
    assert locked.status_code == 401

    with Session() as session:
        row = session.get(UserRecord, user_id)
        row.failed_login_count = 0
        row.locked_until = None
        session.commit()
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "LongPassword123!"}).status_code == 200
    assert client.post("/api/v1/auth/refresh").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 200
    with Session() as session:
        assert session.scalars(select(AuthSessionRecord)).first().revoked_at is not None


def test_viewer_cannot_use_admin_api_and_admin_can_create_user(client, Session):
    viewer = login(client, Session, username="viewer", role="VIEWER")
    assert viewer.status_code == 200
    forbidden = client.get("/api/v1/admin/users")
    assert forbidden.status_code == 403
    client.post("/api/v1/auth/logout")
    assert login(client, Session, username="admin", role="ADMIN").status_code == 200
    created = client.post("/api/v1/admin/users", json={"username": "u2", "password": "AnotherLong123!", "role": "VIEWER"})
    assert created.status_code == 200


def test_eastmoney_spot_normalization_and_invalid_ohlc():
    frame = pd.DataFrame([{"代码": "600519", "名称": "贵州茅台", "最新价": "100", "今开": "99", "最高": "101", "最低": "98", "昨收": "97", "成交量": "1000", "成交额": "100000"}])
    rows = normalize_eastmoney_spot(frame, "eastmoney")
    assert rows[0].symbol == "600519.SH"
    assert rows[0].checksum
    bad = frame.copy()
    bad.loc[0, "最高"] = "90"
    with pytest.raises(ValueError):
        normalize_eastmoney_spot(bad, "eastmoney")


def test_fixture_market_spot_ingestion_is_idempotent(Session, monkeypatch):
    monkeypatch.setattr(data_jobs, "assert_schema_ready_for_writes", lambda _engine: None)
    first = data_jobs.run_market_spot(FixtureProvider())
    second = data_jobs.run_market_spot(FixtureProvider())
    assert first == second == 0
    with Session() as session:
        assert session.query(MarketQuoteSnapshotRecord).count() == 2
        runs = session.scalars(select(DataIngestionRunRecord).order_by(DataIngestionRunRecord.started_at.asc())).all()
        assert runs[0].success_count == 2
        assert runs[1].duplicate_count == 2
