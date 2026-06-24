from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import (
    CurrentUser,
    UserRole,
    change_password,
    clear_auth_cookies,
    create_user,
    login_user,
    logout_user,
    refresh_user_session,
    require_admin,
    require_user,
    hash_password,
    utc_now,
)
from ..db import AuthSessionRecord, SessionLocal, UserRecord

router = APIRouter(prefix="/api/v1", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=12, max_length=200)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=200)
    role: str
    display_name: str = Field(default="", max_length=120)


class PatchUserRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    role: str | None = None
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=200)


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    user = login_user(username=payload.username, password=payload.password, request=request, response=response)
    return ok(request, _current_user(user))


@router.post("/auth/refresh")
def refresh(request: Request, response: Response) -> dict[str, Any]:
    user = refresh_user_session(request=request, response=response)
    return ok(request, _current_user(user))


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    logout_user(request=request, response=response)
    clear_auth_cookies(response)
    return ok(request, {"status": "OK"})


@router.get("/auth/me")
def me(request: Request, user: CurrentUser = Depends(require_user)) -> dict[str, Any]:
    return ok(request, _current_user(user))


@router.post("/auth/change-password")
def api_change_password(payload: ChangePasswordRequest, request: Request, response: Response, user: CurrentUser = Depends(require_user)) -> dict[str, Any]:
    change_password(user_id=user.user_id, old_password=payload.old_password, new_password=payload.new_password)
    clear_auth_cookies(response)
    return ok(request, {"status": "PASSWORD_CHANGED"})


@router.get("/admin/users")
def admin_users(request: Request, page_no: int = 1, page_size: int = 20, _: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    page_size = min(max(page_size, 1), 100)
    with SessionLocal() as session:
        query = select(UserRecord).order_by(UserRecord.id.asc())
        rows = session.scalars(query.offset((page_no - 1) * page_size).limit(page_size)).all()
        total = session.query(UserRecord).count()
    return ok(request, page([_user(row) for row in rows], page_no=page_no, page_size=page_size, total=total))


@router.post("/admin/users")
def admin_create_user(payload: CreateUserRequest, request: Request, _: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    row = create_user(username=payload.username, password=payload.password, role=payload.role, display_name=payload.display_name)
    return ok(request, _user(row))


@router.patch("/admin/users/{user_id}")
def admin_patch_user(user_id: int, payload: PatchUserRequest, request: Request, _: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    now = utc_now()
    with SessionLocal() as session:
        row = session.get(UserRecord, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="user not found")
        if payload.role is not None:
            if payload.role not in {UserRole.ADMIN.value, UserRole.VIEWER.value}:
                raise HTTPException(status_code=422, detail="invalid role")
            row.role = payload.role
        if payload.display_name is not None:
            row.display_name = payload.display_name
        if payload.is_active is not None:
            row.is_active = payload.is_active
            if not payload.is_active:
                session.query(AuthSessionRecord).filter(AuthSessionRecord.user_id == row.id, AuthSessionRecord.revoked_at.is_(None)).update({"revoked_at": now})
        row.updated_at = now
        session.commit()
        session.refresh(row)
        return ok(request, _user(row))


@router.post("/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, payload: ResetPasswordRequest, request: Request, _: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    now = utc_now()
    with SessionLocal() as session:
        row = session.get(UserRecord, user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="user not found")
        row.password_hash = hash_password(payload.password)
        row.password_changed_at = now
        row.updated_at = now
        session.query(AuthSessionRecord).filter(AuthSessionRecord.user_id == row.id, AuthSessionRecord.revoked_at.is_(None)).update({"revoked_at": now})
        session.commit()
    return ok(request, {"status": "PASSWORD_RESET"})


@router.post("/admin/users/{user_id}/revoke-sessions")
def admin_revoke_sessions(user_id: int, request: Request, _: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    now = utc_now()
    with SessionLocal() as session:
        count = session.query(AuthSessionRecord).filter(AuthSessionRecord.user_id == user_id, AuthSessionRecord.revoked_at.is_(None)).update({"revoked_at": now})
        session.commit()
    return ok(request, {"revoked": count})


def _current_user(user: CurrentUser) -> dict[str, Any]:
    return {"user_id": user.user_id, "username": user.username, "role": user.role}


def _user(row: UserRecord) -> dict[str, Any]:
    return {
        "user_id": row.id,
        "username": row.username,
        "display_name": row.display_name,
        "role": row.role,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def ok(request: Request, data: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "request_id": getattr(request.state, "request_id", ""), "environment": "PAPER_TRADING"}


def page(items: list[dict[str, Any]], *, page_no: int, page_size: int, total: int) -> dict[str, Any]:
    return {"items": items, "page": page_no, "page_size": page_size, "total": total}
