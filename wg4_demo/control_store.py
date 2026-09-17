"""Control database schema and integrity checks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from wg4_demo.database import connect_sqlite, transaction
from wg4_demo.errors import ConfigurationError

CONTROL_SCHEMA_VERSION = 2


def initialize_control_store(
    path: Path,
    *,
    hard_call_ceiling: int,
    auth_version: str,
    budget_mode: str,
) -> None:
    connection = connect_sqlite(path)
    try:
        with transaction(connection, immediate=True):
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    allocated_calls INTEGER NOT NULL CHECK (allocated_calls >= 0),
                    hard_call_ceiling INTEGER NOT NULL CHECK (hard_call_ceiling > 0),
                    budget_mode TEXT NOT NULL CHECK (
                        budget_mode IN ('finite', 'provider_hard_limit')
                    ),
                    auth_version TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL CHECK (role IN ('participant', 'admin')),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    auth_version TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    client_token TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS idx_login_attempts_token_time
                    ON login_attempts(client_token, attempted_at);
                CREATE TABLE IF NOT EXISTS api_calls (
                    call_id TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'completed', 'failed', 'unknown')),
                    reserved_at TEXT NOT NULL,
                    completed_at TEXT,
                    estimated_input_tokens INTEGER NOT NULL,
                    reserved_tokens INTEGER NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    safe_error_code TEXT,
                    UNIQUE(action_id, ordinal),
                    FOREIGN KEY(session_id) REFERENCES auth_sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_api_calls_session ON api_calls(session_id);
                CREATE TABLE IF NOT EXISTS rate_reservations (
                    call_id TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL,
                    reserved_tokens INTEGER NOT NULL,
                    actual_tokens INTEGER,
                    FOREIGN KEY(call_id) REFERENCES api_calls(call_id)
                );
                CREATE TABLE IF NOT EXISTS admin_events (
                    id TEXT PRIMARY KEY,
                    actor_session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    amount INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(actor_session_id) REFERENCES auth_sessions(id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL UNIQUE,
                    dedupe_key TEXT NOT NULL,
                    sequence INTEGER NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    auth_version TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    kb_revision INTEGER NOT NULL,
                    model_id TEXT NOT NULL,
                    model_settings_json TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    queue_deadline_at TEXT NOT NULL,
                    started_at TEXT,
                    run_deadline_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    coordinator_epoch TEXT,
                    worker_id TEXT,
                    claim_token TEXT,
                    outcome_id TEXT,
                    safe_error_code TEXT,
                    failure_stage TEXT,
                    UNIQUE(session_id, dedupe_key),
                    FOREIGN KEY(session_id) REFERENCES auth_sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_state_sequence ON jobs(state, sequence);
                CREATE INDEX IF NOT EXISTS idx_jobs_session_state ON jobs(session_id, state);
                CREATE TABLE IF NOT EXISTS job_events (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    safe_error_code TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                """
            )
            row = connection.execute("SELECT * FROM control_state WHERE singleton = 1").fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO control_state (
                        singleton, schema_version, enabled, allocated_calls,
                        hard_call_ceiling, budget_mode, auth_version, updated_at
                    ) VALUES (1, ?, ?, 0, ?, ?, ?, datetime('now'))
                    """,
                    (
                        CONTROL_SCHEMA_VERSION,
                        int(budget_mode == "provider_hard_limit"),
                        hard_call_ceiling,
                        budget_mode,
                        auth_version,
                    ),
                )
                return
            if row["schema_version"] == 1:
                connection.execute(
                    """
                    ALTER TABLE control_state ADD COLUMN budget_mode TEXT NOT NULL
                    DEFAULT 'finite' CHECK (budget_mode IN ('finite', 'provider_hard_limit'))
                    """,
                )
                connection.execute(
                    "UPDATE control_state SET schema_version = ? WHERE singleton = 1",
                    (CONTROL_SCHEMA_VERSION,),
                )
                row = connection.execute(
                    "SELECT * FROM control_state WHERE singleton = 1"
                ).fetchone()
            elif row["schema_version"] != CONTROL_SCHEMA_VERSION:
                raise ConfigurationError("利用台帳のschemaが不明なため、LLM機能を停止しました。")
            if row is None:
                raise ConfigurationError("利用台帳を初期化できませんでした。")
            if row["budget_mode"] != budget_mode:
                connection.execute(
                    """
                    UPDATE control_state
                    SET budget_mode = ?, enabled = ?, auth_version = ?, updated_at = datetime('now')
                    WHERE singleton = 1
                    """,
                    (budget_mode, int(budget_mode == "provider_hard_limit"), auth_version),
                )
            elif row["auth_version"] != auth_version:
                if budget_mode == "finite":
                    connection.execute(
                        """
                        UPDATE control_state
                        SET enabled = 0, auth_version = ?, updated_at = datetime('now')
                        WHERE singleton = 1
                        """,
                        (auth_version,),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE control_state SET auth_version = ?, updated_at = datetime('now')
                        WHERE singleton = 1
                        """,
                        (auth_version,),
                    )
    except sqlite3.DatabaseError as exc:
        raise ConfigurationError("利用台帳を読み取れないため、LLM機能を停止しました。") from exc
    finally:
        connection.close()
