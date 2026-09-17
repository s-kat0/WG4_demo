"""Argon2id authentication with server-side sessions and throttling."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from wg4_demo.control_store import initialize_control_store
from wg4_demo.database import connect_sqlite, transaction
from wg4_demo.errors import AuthenticationError, AuthorizationError
from wg4_demo.schemas import Role, SessionRecord
from wg4_demo.settings import Settings


class AuthService:
    _verification_slots = threading.BoundedSemaphore(2)

    def __init__(self, control_db: Path, settings: Settings) -> None:
        self.control_db = control_db
        self.settings = settings
        initialize_control_store(
            control_db,
            hard_call_ceiling=settings.app_max_llm_calls,
            auth_version=settings.auth_version or "UNCONFIGURED",
            budget_mode=settings.call_budget_mode,
        )
        self.hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)

    def login(
        self,
        password: str,
        *,
        role: Role,
        client_token: str,
        now: datetime | None = None,
    ) -> SessionRecord:
        current = now or datetime.now(UTC)
        password_hash = (
            self.settings.demo_password_hash
            if role is Role.PARTICIPANT
            else self.settings.admin_password_hash
        )
        if password_hash is None or self.settings.auth_version is None:
            raise AuthenticationError("authentication_not_configured", "認証設定が未完了です。")
        self._enforce_attempt_limit(client_token, current)
        if not self._verification_slots.acquire(blocking=False):
            raise AuthenticationError("authentication_busy", "認証処理が混雑しています。")
        succeeded = False
        try:
            try:
                succeeded = self.hasher.verify(password_hash.get_secret_value(), password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                succeeded = False
        finally:
            self._verification_slots.release()
            self._record_attempt(client_token, current, succeeded)
        if not succeeded:
            raise AuthenticationError()
        lifetime = timedelta(hours=8) if role is Role.PARTICIPANT else timedelta(minutes=10)
        session = SessionRecord(
            id=str(uuid4()),
            role=role,
            expires_at=current + lifetime,
            auth_version=self.settings.auth_version,
        )
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                connection.execute(
                    """
                    INSERT INTO auth_sessions (
                        id, role, created_at, expires_at, auth_version, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        session.id,
                        session.role.value,
                        current.isoformat(),
                        session.expires_at.isoformat(),
                        session.auth_version,
                    ),
                )
        finally:
            connection.close()
        return session

    def require_session(
        self,
        session_id: str,
        *,
        role: Role | None = None,
        now: datetime | None = None,
    ) -> SessionRecord:
        current = now or datetime.now(UTC)
        connection = connect_sqlite(self.control_db)
        try:
            row = connection.execute(
                """
                SELECT id, role, expires_at, auth_version, revoked_at
                FROM auth_sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise AuthenticationError(
                "authentication_store_failed", "認証状態を確認できません。"
            ) from exc
        finally:
            connection.close()
        if row is None or row["revoked_at"] is not None:
            raise AuthenticationError("session_invalid", "セッションが無効です。")
        if row["auth_version"] != self.settings.auth_version:
            raise AuthenticationError("session_generation_expired", "再ログインしてください。")
        expires = datetime.fromisoformat(row["expires_at"])
        if current >= expires:
            raise AuthenticationError("session_expired", "セッションの期限が切れました。")
        actual_role = Role(row["role"])
        if role is not None and actual_role is not role:
            raise AuthorizationError()
        return SessionRecord(
            id=row["id"], role=actual_role, expires_at=expires, auth_version=row["auth_version"]
        )

    def logout(self, session_id: str, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                connection.execute(
                    "UPDATE auth_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (current.isoformat(), session_id),
                )
        finally:
            connection.close()

    def _enforce_attempt_limit(self, client_token: str, current: datetime) -> None:
        since = (current - timedelta(seconds=60)).isoformat()
        connection = connect_sqlite(self.control_db)
        try:
            failures = connection.execute(
                """
                SELECT COUNT(*) AS count FROM login_attempts
                WHERE client_token = ? AND attempted_at >= ? AND succeeded = 0
                """,
                (client_token, since),
            ).fetchone()["count"]
            total = connection.execute(
                "SELECT COUNT(*) AS count FROM login_attempts WHERE attempted_at >= ?",
                (since,),
            ).fetchone()["count"]
        finally:
            connection.close()
        if failures >= 5:
            raise AuthenticationError("authentication_cooldown", "60秒後に再試行してください。")
        if total >= 100:
            raise AuthenticationError("authentication_busy", "認証処理が混雑しています。")

    def _record_attempt(self, client_token: str, current: datetime, succeeded: bool) -> None:
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                connection.execute(
                    """
                    INSERT INTO login_attempts (attempt_id, client_token, attempted_at, succeeded)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(uuid4()), client_token, current.isoformat(), int(succeeded)),
                )
        finally:
            connection.close()
