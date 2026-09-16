"""SQLite-backed bounded FIFO queue with owner-only status access."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from wg4_demo.auth import AuthService
from wg4_demo.database import connect_sqlite, transaction
from wg4_demo.errors import AppError, AuthorizationError
from wg4_demo.repository import Repository, canonical_json, sha256_text
from wg4_demo.schemas import JobState, Role
from wg4_demo.settings import Settings

ACTIVE_STATES = {
    JobState.QUEUED.value,
    JobState.RUNNING.value,
    JobState.CANCEL_REQUESTED.value,
}


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    action_id: str
    sequence: int
    session_id: str
    workspace_id: str
    conversation_id: str
    mode: str
    payload: dict[str, Any]
    input_sha256: str
    kb_revision: int
    model_id: str
    model_settings: dict[str, Any]
    prompt_version: str
    schema_version: str
    state: JobState
    created_at: datetime
    queue_deadline_at: datetime
    started_at: datetime | None
    run_deadline_at: datetime | None
    claim_token: str | None
    outcome_id: str | None
    safe_error_code: str | None
    failure_stage: str | None
    queue_position: int | None = None


class JobService:
    def __init__(
        self,
        control_db: Path,
        settings: Settings,
        auth: AuthService,
        repository: Repository,
        *,
        coordinator_epoch: str | None = None,
    ) -> None:
        self.control_db = control_db
        self.settings = settings
        self.auth = auth
        self.repository = repository
        self.coordinator_epoch = coordinator_epoch or str(uuid4())

    def recover_interrupted_jobs(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        connection = connect_sqlite(self.control_db)
        interrupted: list[JobRecord] = []
        try:
            with transaction(connection, immediate=True):
                rows = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE state IN ('queued', 'running', 'cancel_requested')
                      AND coordinator_epoch <> ?
                    """,
                    (self.coordinator_epoch,),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE jobs SET state = 'indeterminate', finished_at = ?,
                            safe_error_code = 'process_restarted', failure_stage = 'scheduler'
                        WHERE job_id = ?
                        """,
                        (current.isoformat(), row["job_id"]),
                    )
                    updated = connection.execute(
                        "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                    ).fetchone()
                    interrupted.append(self._job_from_row(updated))
        finally:
            connection.close()
        for job in interrupted:
            self.repository.invalidate_action_guard(job.workspace_id, job.action_id)
            self.repository.abort_staged_by_action(job.workspace_id, job.action_id)
            self._record_comparison_failure(job, "process_restarted")
        return len(interrupted)

    def enqueue(
        self,
        *,
        session_id: str,
        workspace_id: str,
        conversation_id: str,
        mode: str,
        payload: dict[str, Any],
        kb_revision: int,
        model_id: str,
        model_settings: dict[str, Any],
        prompt_version: str,
        schema_version: str,
        dedupe_key: str,
        now: datetime | None = None,
    ) -> JobRecord:
        current = now or datetime.now(UTC)
        session = self.auth.require_session(session_id, role=Role.PARTICIPANT, now=current)
        workspace = self.repository.require_workspace(session_id, workspace_id)
        if workspace.kb_revision != kb_revision:
            raise AppError("stale_context", "知識版が変わっています。", "enqueue")
        frozen_payload = canonical_json(payload)
        input_sha256 = sha256_text(frozen_payload)
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                existing = connection.execute(
                    """
                    SELECT * FROM jobs WHERE session_id = ? AND dedupe_key = ?
                    """,
                    (session_id, dedupe_key),
                ).fetchone()
                if existing is not None:
                    return self._job_from_row(
                        existing, queue_position=self._position(connection, existing)
                    )
                active = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM jobs
                    WHERE session_id = ? AND state IN ('queued', 'running', 'cancel_requested')
                    """,
                    (session_id,),
                ).fetchone()["count"]
                if active >= self.settings.max_active_jobs_per_session:
                    raise AppError(
                        "session_job_active",
                        "このセッションには待機中または実行中の操作があります。",
                        "queue",
                    )
                pending = connection.execute(
                    "SELECT COUNT(*) AS count FROM jobs WHERE state = 'queued'"
                ).fetchone()["count"]
                if pending >= self.settings.max_pending_jobs:
                    raise AppError("queue_full", "待機列が満杯です。", "queue")
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM jobs"
                ).fetchone()["next_sequence"]
                action_id = str(uuid4())
                job_id = str(uuid4())
                deadline = current + timedelta(seconds=self.settings.queue_wait_timeout_seconds)
                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, action_id, dedupe_key, sequence, session_id, workspace_id,
                        conversation_id, auth_version, mode, payload_json, input_sha256,
                        kb_revision, model_id, model_settings_json, prompt_version,
                        schema_version, state, created_at, queue_deadline_at, coordinator_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'queued', ?, ?, ?)
                    """,
                    (
                        job_id,
                        action_id,
                        dedupe_key,
                        sequence,
                        session_id,
                        workspace_id,
                        conversation_id,
                        session.auth_version,
                        mode,
                        frozen_payload,
                        input_sha256,
                        kb_revision,
                        model_id,
                        canonical_json(model_settings),
                        prompt_version,
                        schema_version,
                        current.isoformat(),
                        deadline.isoformat(),
                        self.coordinator_epoch,
                    ),
                )
                self._event(connection, job_id, JobState.QUEUED, None, current)
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                return self._job_from_row(row, queue_position=self._position(connection, row))
        finally:
            connection.close()

    def claim_next(self, worker_id: str, *, now: datetime | None = None) -> JobRecord | None:
        current = now or datetime.now(UTC)
        connection = connect_sqlite(self.control_db)
        rejected: list[JobRecord] = []
        claimed_job: JobRecord | None = None
        try:
            with transaction(connection, immediate=True):
                running = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM jobs
                    WHERE state IN ('running', 'cancel_requested')
                    """
                ).fetchone()["count"]
                while running < self.settings.max_concurrent_jobs:
                    row = connection.execute(
                        """
                        SELECT j.*, a.expires_at AS session_expires_at,
                               a.revoked_at AS session_revoked_at
                        FROM jobs j JOIN auth_sessions a ON a.id = j.session_id
                        WHERE j.state = 'queued' AND j.coordinator_epoch = ?
                        ORDER BY j.sequence LIMIT 1
                        """,
                        (self.coordinator_epoch,),
                    ).fetchone()
                    if row is None:
                        break
                    reason = self._dequeue_rejection(row, current)
                    if reason is not None:
                        state, code = reason
                        connection.execute(
                            """
                            UPDATE jobs SET state = ?, finished_at = ?, safe_error_code = ?,
                                failure_stage = 'dequeue'
                            WHERE job_id = ? AND state = 'queued'
                            """,
                            (state.value, current.isoformat(), code, row["job_id"]),
                        )
                        self._event(connection, row["job_id"], state, code, current)
                        updated = connection.execute(
                            "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                        ).fetchone()
                        rejected.append(self._job_from_row(updated))
                        continue
                    claim_token = str(uuid4())
                    run_deadline = current + timedelta(seconds=self.settings.action_timeout_seconds)
                    changed = connection.execute(
                        """
                        UPDATE jobs SET state = 'running', started_at = ?, run_deadline_at = ?,
                            heartbeat_at = ?, worker_id = ?, claim_token = ?
                        WHERE job_id = ? AND state = 'queued'
                        """,
                        (
                            current.isoformat(),
                            run_deadline.isoformat(),
                            current.isoformat(),
                            worker_id,
                            claim_token,
                            row["job_id"],
                        ),
                    ).rowcount
                    if changed != 1:
                        continue
                    try:
                        self.repository.begin_action_guard(
                            row["workspace_id"],
                            action_id=row["action_id"],
                            claim_token=claim_token,
                            kb_revision=row["kb_revision"],
                        )
                    except AppError as exc:
                        if exc.code != "stale_context":
                            raise
                        connection.execute(
                            """
                            UPDATE jobs SET state = 'stale_context', finished_at = ?,
                                safe_error_code = 'stale_context', failure_stage = 'dequeue'
                            WHERE job_id = ? AND state = 'running' AND claim_token = ?
                            """,
                            (current.isoformat(), row["job_id"], claim_token),
                        )
                        self._event(
                            connection,
                            row["job_id"],
                            JobState.STALE_CONTEXT,
                            "stale_context",
                            current,
                        )
                        updated = connection.execute(
                            "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                        ).fetchone()
                        rejected.append(self._job_from_row(updated))
                        continue
                    claimed = connection.execute(
                        "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                    ).fetchone()
                    self._event(connection, row["job_id"], JobState.RUNNING, None, current)
                    claimed_job = self._job_from_row(claimed)
                    break
        finally:
            connection.close()
        for job in rejected:
            self._record_comparison_failure(job, job.safe_error_code or job.state.value)
        return claimed_job

    def cancel(self, *, session_id: str, job_id: str, now: datetime | None = None) -> JobRecord:
        current = now or datetime.now(UTC)
        self.auth.require_session(session_id, role=Role.PARTICIPANT, now=current)
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ? AND session_id = ?",
                    (job_id, session_id),
                ).fetchone()
                if row is None:
                    raise AuthorizationError("指定されたジョブを取得できません。")
                if row["state"] == JobState.QUEUED.value:
                    connection.execute(
                        """
                        UPDATE jobs SET state = 'cancelled', finished_at = ?,
                            safe_error_code = 'cancelled_before_start'
                        WHERE job_id = ? AND state = 'queued'
                        """,
                        (current.isoformat(), job_id),
                    )
                    self._event(
                        connection,
                        job_id,
                        JobState.CANCELLED,
                        "cancelled_before_start",
                        current,
                    )
                elif row["state"] == JobState.RUNNING.value:
                    outcome = self.repository.get_action_outcome(
                        row["workspace_id"], row["action_id"], session_id=session_id
                    )
                    if outcome is None:
                        self.repository.invalidate_action_guard(
                            row["workspace_id"], row["action_id"]
                        )
                        connection.execute(
                            "UPDATE jobs SET state = 'cancel_requested' WHERE job_id = ? AND state = 'running'",
                            (job_id,),
                        )
                        self._event(connection, job_id, JobState.CANCEL_REQUESTED, None, current)
                updated = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                return self._job_from_row(
                    updated, queue_position=self._position(connection, updated)
                )
        finally:
            connection.close()

    def get(self, *, session_id: str, job_id: str) -> JobRecord:
        connection = connect_sqlite(self.control_db)
        try:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND session_id = ?",
                (job_id, session_id),
            ).fetchone()
            if row is None:
                raise AuthorizationError("指定されたジョブを取得できません。")
            return self._job_from_row(row, queue_position=self._position(connection, row))
        finally:
            connection.close()

    def heartbeat(self, job: JobRecord) -> bool:
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                row = connection.execute(
                    "SELECT state, run_deadline_at, claim_token FROM jobs WHERE job_id = ?",
                    (job.job_id,),
                ).fetchone()
                if row is None or row["claim_token"] != job.claim_token:
                    return False
                if row["state"] != JobState.RUNNING.value:
                    return False
                deadline = datetime.fromisoformat(row["run_deadline_at"])
                now = datetime.now(UTC)
                if now >= deadline:
                    return False
                connection.execute(
                    "UPDATE jobs SET heartbeat_at = ? WHERE job_id = ?",
                    (now.isoformat(), job.job_id),
                )
                return True
        finally:
            connection.close()

    def succeed(self, job: JobRecord, outcome_id: str, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        connection = connect_sqlite(self.control_db)
        try:
            with transaction(connection, immediate=True):
                row = connection.execute(
                    "SELECT state, claim_token FROM jobs WHERE job_id = ?", (job.job_id,)
                ).fetchone()
                if row is None or row["claim_token"] != job.claim_token:
                    raise AppError("job_claim_lost", "ジョブの実行権を確認できません。", "job")
                if row["state"] == JobState.CANCEL_REQUESTED.value:
                    self._finish_cancelled(connection, job, current)
                    return
                if row["state"] != JobState.RUNNING.value:
                    raise AppError("job_state_conflict", "ジョブ状態を確定できません。", "job")
                connection.execute(
                    """
                    UPDATE jobs SET state = 'succeeded', outcome_id = ?, finished_at = ?
                    WHERE job_id = ? AND state = 'running' AND claim_token = ?
                    """,
                    (outcome_id, current.isoformat(), job.job_id, job.claim_token),
                )
                self._event(connection, job.job_id, JobState.SUCCEEDED, None, current)
        finally:
            connection.close()

    def fail(
        self,
        job: JobRecord,
        *,
        state: JobState,
        safe_error_code: str,
        failure_stage: str,
        now: datetime | None = None,
    ) -> None:
        if state not in {
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.EXPIRED,
            JobState.INDETERMINATE,
            JobState.STALE_CONTEXT,
        }:
            raise ValueError("state must be terminal")
        current = now or datetime.now(UTC)
        self.repository.invalidate_action_guard(job.workspace_id, job.action_id)
        self.repository.abort_staged_by_action(job.workspace_id, job.action_id)
        connection = connect_sqlite(self.control_db)
        changed = 0
        try:
            with transaction(connection, immediate=True):
                changed = connection.execute(
                    """
                    UPDATE jobs SET state = ?, finished_at = ?, safe_error_code = ?,
                        failure_stage = ?
                    WHERE job_id = ? AND claim_token = ?
                      AND state IN ('running', 'cancel_requested')
                    """,
                    (
                        state.value,
                        current.isoformat(),
                        safe_error_code,
                        failure_stage,
                        job.job_id,
                        job.claim_token,
                    ),
                ).rowcount
                if changed == 1:
                    self._event(connection, job.job_id, state, safe_error_code, current)
        finally:
            connection.close()
        if changed == 1:
            self._record_comparison_failure(job, safe_error_code)

    def _record_comparison_failure(self, job: JobRecord, safe_error_code: str) -> None:
        comparison_stage = job.payload.get("comparison_stage")
        if comparison_stage is None:
            return
        self.repository.record_answer_snapshot_failure(
            job.workspace_id,
            action_id=job.action_id,
            comparison={
                "stage": str(comparison_stage),
                "question": str(job.payload.get("question", "")),
                "conversation_id": job.conversation_id,
                "empty_history": job.payload.get("empty_history") is True,
                "kb_revision": job.kb_revision,
                "target_item_id": job.payload.get("target_item_id"),
                "target_version": job.payload.get("target_version"),
                "model_id": job.model_id,
                "model_settings": job.model_settings,
                "prompt_version": job.prompt_version,
                "schema_version": job.schema_version,
                "retrieval_version": "wg4-lexical-v2",
            },
            safe_error_code=safe_error_code,
        )

    def _finish_cancelled(
        self, connection: sqlite3.Connection, job: JobRecord, current: datetime
    ) -> None:
        connection.execute(
            """
            UPDATE jobs SET state = 'cancelled', finished_at = ?,
                safe_error_code = 'cancelled_during_run'
            WHERE job_id = ? AND state = 'cancel_requested'
            """,
            (current.isoformat(), job.job_id),
        )
        self._event(connection, job.job_id, JobState.CANCELLED, "cancelled_during_run", current)

    def _dequeue_rejection(
        self, row: sqlite3.Row, current: datetime
    ) -> tuple[JobState, str] | None:
        if current >= datetime.fromisoformat(row["queue_deadline_at"]):
            return JobState.EXPIRED, "queue_wait_timeout"
        if row["session_revoked_at"] is not None:
            return JobState.FAILED, "session_revoked"
        if current >= datetime.fromisoformat(row["session_expires_at"]):
            return JobState.FAILED, "session_expired"
        if row["auth_version"] != self.settings.auth_version:
            return JobState.FAILED, "auth_version_mismatch"
        if self.settings.demo_expires_at and current >= self.settings.demo_expires_at:
            return JobState.FAILED, "demo_expired"
        return None

    def _position(self, connection: sqlite3.Connection, row: sqlite3.Row | None) -> int | None:
        if row is None or row["state"] != JobState.QUEUED.value:
            return None
        return int(
            connection.execute(
                """
                SELECT COUNT(*) AS count FROM jobs
                WHERE state = 'queued' AND sequence < ?
                """,
                (row["sequence"],),
            ).fetchone()["count"]
        )

    def _event(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        state: JobState,
        safe_error_code: str | None,
        current: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events (event_id, job_id, state, safe_error_code, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(uuid4()), job_id, state.value, safe_error_code, current.isoformat()),
        )

    def _job_from_row(
        self, row: sqlite3.Row | None, *, queue_position: int | None = None
    ) -> JobRecord:
        if row is None:
            raise RuntimeError("job row missing")
        return JobRecord(
            job_id=row["job_id"],
            action_id=row["action_id"],
            sequence=row["sequence"],
            session_id=row["session_id"],
            workspace_id=row["workspace_id"],
            conversation_id=row["conversation_id"],
            mode=row["mode"],
            payload=json.loads(row["payload_json"]),
            input_sha256=row["input_sha256"],
            kb_revision=row["kb_revision"],
            model_id=row["model_id"],
            model_settings=json.loads(row["model_settings_json"]),
            prompt_version=row["prompt_version"],
            schema_version=row["schema_version"],
            state=JobState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            queue_deadline_at=datetime.fromisoformat(row["queue_deadline_at"]),
            started_at=(datetime.fromisoformat(row["started_at"]) if row["started_at"] else None),
            run_deadline_at=(
                datetime.fromisoformat(row["run_deadline_at"]) if row["run_deadline_at"] else None
            ),
            claim_token=row["claim_token"],
            outcome_id=row["outcome_id"],
            safe_error_code=row["safe_error_code"],
            failure_stage=row["failure_stage"],
            queue_position=queue_position,
        )
