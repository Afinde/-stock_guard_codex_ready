from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from .auth import UserRole, create_user, hash_password, utc_now
from .db import AuthSessionRecord, SessionLocal, UserRecord
from .schema import assert_schema_ready_for_writes
from .db import engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stock Guard administration CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    create_admin = sub.add_parser("create-admin")
    create_admin.add_argument("--username", required=True)
    create_admin.add_argument("--display-name", default="")
    create_user_parser = sub.add_parser("create-user")
    create_user_parser.add_argument("--username", required=True)
    create_user_parser.add_argument("--role", choices=[UserRole.ADMIN.value, UserRole.VIEWER.value], default=UserRole.VIEWER.value)
    create_user_parser.add_argument("--display-name", default="")
    reset = sub.add_parser("reset-password")
    reset.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    assert_schema_ready_for_writes(engine)
    if args.command == "create-admin":
        password = _read_password()
        row = create_user(username=args.username, password=password, role=UserRole.ADMIN.value, display_name=args.display_name)
        print(f"created admin user: {row.username}")
        return 0
    if args.command == "create-user":
        password = _read_password()
        row = create_user(username=args.username, password=password, role=args.role, display_name=args.display_name)
        print(f"created user: {row.username}")
        return 0
    if args.command == "reset-password":
        password = _read_password()
        _reset_password(args.username, password)
        print(f"password reset for user: {args.username.strip().lower()}")
        return 0
    return 2


def _read_password() -> str:
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("passwords do not match")
    if len(password) < 12:
        raise SystemExit("password must be at least 12 characters")
    return password


def _reset_password(username: str, password: str) -> None:
    now = utc_now()
    with SessionLocal() as session:
        row = session.scalars(select(UserRecord).where(UserRecord.username == username.strip().lower())).first()
        if row is None:
            raise SystemExit("user not found")
        row.password_hash = hash_password(password)
        row.password_changed_at = now
        row.updated_at = now
        session.query(AuthSessionRecord).filter(AuthSessionRecord.user_id == row.id, AuthSessionRecord.revoked_at.is_(None)).update({"revoked_at": now})
        session.commit()


if __name__ == "__main__":
    raise SystemExit(main())
