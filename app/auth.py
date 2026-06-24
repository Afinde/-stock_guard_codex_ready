from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, Response
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import AuthSessionRecord, LoginAuditLogRecord, SessionLocal, UserRecord

ACCESS_COOKIE = "sg_access_token"
REFRESH_COOKIE = "sg_refresh_token"
JWT_ALGORITHM = "HS256"
GENERIC_LOGIN_ERROR = "invalid username or password"
password_hash = PasswordHash.recommended()


class UserRole(str, Enum):
    ADMIN = "ADMIN"
    VIEWER = "VIEWER"


@dataclass(frozen=True)
class CurrentUser:
    user_id: int
    username: str
    role: str
    session_id: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return password_hash.verify(password, hashed)
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(*, user: UserRecord, session_id: str) -> str:
    settings = get_settings()
    now = utc_now()
    payload = {
        "sub": str(user.id),
        "sid": session_id,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.auth_access_token_minutes)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def set_auth_cookies(response: Response, *, access_token: str, refresh_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=settings.auth_access_token_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=settings.auth_refresh_token_days * 24 * 3600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/api/v1/auth",
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


def login_user(
    *,
    username: str,
    password: str,
    request: Request,
    response: Response,
    session_factory=None,
) -> CurrentUser:
    session_factory = session_factory or SessionLocal
    settings = get_settings()
    now = utc_now()
    normalized = username.strip().lower()
    with session_factory() as session:
        user = session.scalars(select(UserRecord).where(UserRecord.username == normalized)).first()
        if user is None:
            _audit(session, username=normalized, user_id=None, success=False, reason="INVALID_CREDENTIALS", request=request, now=now)
            session.commit()
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)
        if _is_locked(user, now):
            _audit(session, username=normalized, user_id=user.id, success=False, reason="LOCKED", request=request, now=now)
            session.commit()
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)
        if not user.is_active:
            _audit(session, username=normalized, user_id=user.id, success=False, reason="DISABLED", request=request, now=now)
            session.commit()
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)
        if not verify_password(password, user.password_hash):
            user.failed_login_count += 1
            if user.failed_login_count >= settings.auth_login_failure_limit:
                user.locked_until = now + timedelta(minutes=settings.auth_lockout_minutes)
            user.updated_at = now
            _audit(session, username=normalized, user_id=user.id, success=False, reason="INVALID_CREDENTIALS", request=request, now=now)
            session.commit()
            raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

        user.failed_login_count = 0
        user.locked_until = None
        user.updated_at = now
        session_id = uuid.uuid4().hex
        refresh_token = create_refresh_token()
        auth_session = AuthSessionRecord(
            session_id=session_id,
            user_id=user.id,
            refresh_token_hash=token_hash(refresh_token),
            issued_at=now,
            expires_at=now + timedelta(days=settings.auth_refresh_token_days),
            user_agent=request.headers.get("user-agent", "")[:300],
            ip_address=_client_ip(request),
        )
        session.add(auth_session)
        _audit(session, username=normalized, user_id=user.id, success=True, reason="LOGIN", request=request, now=now)
        session.commit()
        access_token = create_access_token(user=user, session_id=session_id)
        set_auth_cookies(response, access_token=access_token, refresh_token=refresh_token)
        return CurrentUser(user_id=user.id, username=user.username, role=user.role, session_id=session_id)


def refresh_user_session(*, request: Request, response: Response, session_factory=None) -> CurrentUser:
    session_factory = session_factory or SessionLocal
    settings = get_settings()
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
    now = utc_now()
    with session_factory() as session:
        row = session.scalars(select(AuthSessionRecord).where(AuthSessionRecord.refresh_token_hash == token_hash(refresh_token))).first()
        if row is None or row.revoked_at is not None or (_aware_utc(row.expires_at) or now) <= now:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
        user = session.get(UserRecord, row.user_id)
        if user is None or not user.is_active:
            row.revoked_at = now
            session.commit()
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
        new_refresh = create_refresh_token()
        row.refresh_token_hash = token_hash(new_refresh)
        row.issued_at = now
        row.expires_at = now + timedelta(days=settings.auth_refresh_token_days)
        session.commit()
        access = create_access_token(user=user, session_id=row.session_id)
        set_auth_cookies(response, access_token=access, refresh_token=new_refresh)
        return CurrentUser(user_id=user.id, username=user.username, role=user.role, session_id=row.session_id)


def logout_user(*, request: Request, response: Response, session_factory=None) -> None:
    session_factory = session_factory or SessionLocal
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    now = utc_now()
    if refresh_token:
        with session_factory() as session:
            row = session.scalars(select(AuthSessionRecord).where(AuthSessionRecord.refresh_token_hash == token_hash(refresh_token))).first()
            if row is not None and row.revoked_at is None:
                row.revoked_at = now
                session.commit()
    clear_auth_cookies(response)


def require_user(request: Request) -> CurrentUser:
    settings = get_settings()
    if not settings.auth_required:
        return CurrentUser(user_id=0, username="dev", role=UserRole.ADMIN.value, session_id="dev")
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
    try:
        payload: dict[str, Any] = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="AUTH_REQUIRED") from exc
    user_id = int(payload.get("sub", 0))
    session_id = str(payload.get("sid", ""))
    with SessionLocal() as session:
        user = session.get(UserRecord, user_id)
        row = session.scalars(select(AuthSessionRecord).where(AuthSessionRecord.session_id == session_id)).first()
        now = utc_now()
        if user is None or row is None or row.revoked_at is not None or (_aware_utc(row.expires_at) or now) <= now or not user.is_active:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
        return CurrentUser(user_id=user.id, username=user.username, role=user.role, session_id=session_id)


def require_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    return user


def change_password(*, user_id: int, old_password: str, new_password: str, session_factory=None) -> None:
    session_factory = session_factory or SessionLocal
    now = utc_now()
    with session_factory() as session:
        user = session.get(UserRecord, user_id)
        if user is None or not verify_password(old_password, user.password_hash):
            raise HTTPException(status_code=400, detail="invalid password")
        user.password_hash = hash_password(new_password)
        user.password_changed_at = now
        user.updated_at = now
        session.query(AuthSessionRecord).filter(AuthSessionRecord.user_id == user.id, AuthSessionRecord.revoked_at.is_(None)).update({"revoked_at": now})
        session.commit()


def create_user(*, username: str, password: str, role: str, display_name: str = "", session_factory=None) -> UserRecord:
    session_factory = session_factory or SessionLocal
    normalized = username.strip().lower()
    if role not in {UserRole.ADMIN.value, UserRole.VIEWER.value}:
        raise ValueError("invalid role")
    now = utc_now()
    with session_factory() as session:
        row = UserRecord(
            username=normalized,
            display_name=display_name,
            role=role,
            password_hash=hash_password(password),
            password_changed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def _audit(session: Session, *, username: str, user_id: int | None, success: bool, reason: str, request: Request, now: datetime) -> None:
    session.add(
        LoginAuditLogRecord(
            username=username,
            user_id=user_id,
            success=success,
            reason=reason,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent", "")[:300],
            created_at=now,
        )
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:80]
    return (request.client.host if request.client else "")[:80]


def _is_locked(user: UserRecord, now: datetime) -> bool:
    locked_until = _aware_utc(user.locked_until)
    return locked_until is not None and locked_until > now


def _jwt_secret() -> str:
    settings = get_settings()
    if settings.auth_jwt_secret:
        return settings.auth_jwt_secret
    return "dev-only-change-me-dev-only-change-me"
