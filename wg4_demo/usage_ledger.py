"""Atomic API-call reservations. Sent calls are never refunded automatically."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from wg4_demo.auth import AuthService
from wg4_demo.control_store import initialize_control_store
from wg4_demo.database import connect_sqlite, transaction
from wg4_demo.errors import AppError, AuthorizationError, ConfigurationError
from wg4_demo.schemas import Role
from wg4_demo.settings import Settings


@dataclass(frozen=True, slots=True)
class CallReservation:
    call_id: str
    action_id: str
    ordinal: int
    reserved_tokens: int


class UsageLedger:
    def __init__(self, control_db: Path, settings: Settings, auth: AuthService) -> None:
        self.control_db = control_db
        self.settings = settings
        self.auth = auth
        initialize_control_store(
            control_db,
            hard_call_ceiling=settings.app_max_llm_calls,
            auth_version=settings.auth_version or "UNCONFIGURED",
        )

    def status(self) -> dict[str, int | bool | str]:
        connection = connect_sqlite(self.control_db)
        try:
            state = connection.execute("SELECT * FROM control_state WHERE singleton = 1").fetchone()
            if state is None:
                raise ConfigurationError("利用台帳が存在しないためLLM機能を停止しました。")
            used = connection.execute("SELECT COUNT(*) AS count FROM api_calls").fetchone()["count"]
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM api_calls WHERE state = 'reserved'"
            ).fetchone()["count"]
            return {
                "enabled": bool(state["enabled"]),
                "allocated_calls": state["allocated_calls"],
                "used_calls": used,
                "active_calls": active,
                "auth_version": state["auth_version"],
            }
        except sqlite3.DatabaseError as exc:
            raise ConfigurationError("利用台帳を確認できないためLLM機能を停止しました。") from exc
        finally:
            connection.close()

    def enable_budget(
        self,
        admin_session_id: str,
        *,
        additional_calls: int,
        confirmed_external_limit: bool,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(UTC)
        self.auth.require_session(admin_session_id, role=Role.ADMIN, now=current)
        if not confirmed_external_limit:
            raise AuthorizationError("外部の強制停止型支出上限の確認が必要です。")
        if additional_calls <= 0:
            raise ValueError("additional_calls must be positive")
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                row = connection.execute(
                    "SELECT allocated_calls, hard_call_ceiling FROM control_state WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    raise ConfigurationError("利用台帳がありません。")
                allocation = row["allocated_calls"] + additional_calls
                if allocation > row["hard_call_ceiling"]:
                    raise AppError(
                        "allocation_exceeds_ceiling",
                        "アプリ全体の上限を超える利用枠は追加できません。",
                        "budget",
                    )
                connection.execute(
                    """
                    UPDATE control_state SET enabled = 1, allocated_calls = ?, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (allocation, current.isoformat()),
                )
                connection.execute(
                    """
                    INSERT INTO admin_events (id, actor_session_id, event_type, reason, amount, created_at)
                    VALUES (?, ?, 'enable_or_add', ?, ?, ?)
                    """,
                    (str(uuid4()), admin_session_id, reason, additional_calls, current.isoformat()),
                )
        finally:
            connection.close()

    def stop(self, admin_session_id: str, *, reason: str, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        self.auth.require_session(admin_session_id, role=Role.ADMIN, now=current)
        self._disable(admin_session_id, reason, current)

    def _disable(self, actor_session_id: str, reason: str, current: datetime) -> None:
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                connection.execute(
                    "UPDATE control_state SET enabled = 0, updated_at = ? WHERE singleton = 1",
                    (current.isoformat(),),
                )
                connection.execute(
                    """
                    INSERT INTO admin_events (id, actor_session_id, event_type, reason, amount, created_at)
                    VALUES (?, ?, 'stop', ?, NULL, ?)
                    """,
                    (str(uuid4()), actor_session_id, reason, current.isoformat()),
                )
        finally:
            connection.close()

    def reserve_call(
        self,
        *,
        session_id: str,
        action_id: str,
        model: str,
        estimated_input_tokens: int,
        now: datetime | None = None,
    ) -> CallReservation:
        current = now or datetime.now(UTC)
        if self.settings.live_blockers(now=current):
            raise ConfigurationError("実API送信条件がそろっていないためLLM機能を停止しました。")
        self.auth.require_session(session_id, role=Role.PARTICIPANT, now=current)
        if model != self.settings.openai_model:
            raise AppError("model_not_allowed", "設定されたモデル以外は利用できません。", "policy")
        if estimated_input_tokens > self.settings.max_estimated_input_tokens:
            raise AppError("input_too_large", "入力が上限を超えています。", "policy")
        reserved_tokens = estimated_input_tokens + self.settings.max_output_tokens
        if self.settings.global_tpm is None:
            raise ConfigurationError("GLOBAL_TPMが未設定のためLLM機能を停止しました。")
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                state = connection.execute(
                    "SELECT * FROM control_state WHERE singleton = 1"
                ).fetchone()
                if state is None or not state["enabled"]:
                    raise AppError("llm_disabled", "LLM機能は現在停止中です。", "budget")
                if state["auth_version"] != self.settings.auth_version:
                    raise AppError("auth_version_mismatch", "認証設定が更新されました。", "budget")
                used = connection.execute("SELECT COUNT(*) AS count FROM api_calls").fetchone()[
                    "count"
                ]
                if used >= state["allocated_calls"] or used >= state["hard_call_ceiling"]:
                    raise AppError("call_budget_exhausted", "LLM利用枠を使い切りました。", "budget")
                session_used = connection.execute(
                    "SELECT COUNT(*) AS count FROM api_calls WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["count"]
                if session_used >= self.settings.session_max_llm_calls:
                    raise AppError(
                        "session_budget_exhausted", "このセッションの利用上限です。", "budget"
                    )
                action_used = connection.execute(
                    "SELECT COUNT(*) AS count FROM api_calls WHERE action_id = ?",
                    (action_id,),
                ).fetchone()["count"]
                if action_used >= self.settings.max_model_calls_per_action:
                    raise AppError(
                        "action_call_limit", "この操作のモデル呼出し上限です。", "budget"
                    )
                active = connection.execute(
                    "SELECT COUNT(*) AS count FROM api_calls WHERE state = 'reserved'"
                ).fetchone()["count"]
                if active >= self.settings.max_concurrent_llm:
                    raise AppError(
                        "llm_concurrency_busy", "API送信枠を待っています。", "rate_limit"
                    )
                since = (current - timedelta(seconds=60)).isoformat()
                rate = connection.execute(
                    """
                    SELECT COUNT(*) AS requests, COALESCE(SUM(reserved_tokens), 0) AS tokens
                    FROM rate_reservations WHERE sent_at >= ?
                    """,
                    (since,),
                ).fetchone()
                if rate["requests"] >= self.settings.global_rpm:
                    raise AppError("rpm_wait", "API送信レートの空きを待っています。", "rate_limit")
                if rate["tokens"] + reserved_tokens > self.settings.global_tpm:
                    raise AppError("tpm_wait", "トークン送信枠の空きを待っています。", "rate_limit")
                ordinal = action_used + 1
                call_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO api_calls (
                        call_id, action_id, session_id, ordinal, model, state, reserved_at,
                        estimated_input_tokens, reserved_tokens
                    ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
                    """,
                    (
                        call_id,
                        action_id,
                        session_id,
                        ordinal,
                        model,
                        current.isoformat(),
                        estimated_input_tokens,
                        reserved_tokens,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO rate_reservations (call_id, sent_at, reserved_tokens)
                    VALUES (?, ?, ?)
                    """,
                    (call_id, current.isoformat(), reserved_tokens),
                )
                return CallReservation(call_id, action_id, ordinal, reserved_tokens)
        except sqlite3.DatabaseError as exc:
            raise ConfigurationError("利用台帳を更新できないためAPIを送信しません。") from exc
        finally:
            connection.close()

    async def reserve_call_with_wait(
        self,
        *,
        session_id: str,
        action_id: str,
        model: str,
        estimated_input_tokens: int,
        deadline: datetime,
    ) -> CallReservation:
        while True:
            now = datetime.now(UTC)
            if now >= deadline:
                raise AppError(
                    "action_timeout_before_send", "API送信前に実行期限を迎えました。", "rate_limit"
                )
            try:
                return self.reserve_call(
                    session_id=session_id,
                    action_id=action_id,
                    model=model,
                    estimated_input_tokens=estimated_input_tokens,
                    now=now,
                )
            except AppError as exc:
                if exc.code not in {"llm_concurrency_busy", "rpm_wait", "tpm_wait"}:
                    raise
                await asyncio.sleep(min(0.2, max((deadline - now).total_seconds(), 0.0)))

    def finish_call(
        self,
        reservation: CallReservation,
        *,
        state: str,
        input_tokens: int | None,
        output_tokens: int | None,
        safe_error_code: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if state not in {"completed", "failed", "unknown"}:
            raise ValueError("invalid final call state")
        current = now or datetime.now(UTC)
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                changed = connection.execute(
                    """
                    UPDATE api_calls
                    SET state = ?, completed_at = ?, input_tokens = ?, output_tokens = ?,
                        safe_error_code = ?
                    WHERE call_id = ? AND state = 'reserved'
                    """,
                    (
                        state,
                        current.isoformat(),
                        input_tokens,
                        output_tokens,
                        safe_error_code,
                        reservation.call_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise AppError(
                        "call_state_conflict", "API台帳の状態を確定できません。", "ledger"
                    )
                if input_tokens is not None and output_tokens is not None:
                    connection.execute(
                        "UPDATE rate_reservations SET actual_tokens = ? WHERE call_id = ?",
                        (input_tokens + output_tokens, reservation.call_id),
                    )
        finally:
            connection.close()

    def stop_for_provider_limit(self, reservation: CallReservation, code: str) -> None:
        self.finish_call(
            reservation,
            state="failed",
            input_tokens=None,
            output_tokens=None,
            safe_error_code=code,
        )
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                connection.execute(
                    "UPDATE control_state SET enabled = 0, updated_at = ? WHERE singleton = 1",
                    (datetime.now(UTC).isoformat(),),
                )
        finally:
            connection.close()
