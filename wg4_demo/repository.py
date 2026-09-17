"""Workspace-scoped SQLite repository and atomic proposal approval."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from wg4_demo.database import connect_sqlite, transaction
from wg4_demo.errors import AppError, AuthorizationError, StaleContextError, ValidationFailure
from wg4_demo.schemas import (
    CauseStatus,
    ConditionScope,
    ConversationState,
    EvidenceDraft,
    EvidenceRef,
    FactDraft,
    FactKind,
    OperationType,
    ProposalOperation,
    StoredFact,
)
from wg4_demo.seed_v5 import V5SeedManifest, load_v5_seed

DOMAIN_SCHEMA_VERSION = 2
stored_facts_adapter = TypeAdapter(list[StoredFact])
operations_adapter = TypeAdapter(list[ProposalOperation])


class _StaleProposalError(Exception):
    """Internal signal used to commit a stale transition before returning an error."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    id: str
    session_id: str
    kb_revision: int
    seed_mode: str
    lecture_case_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    workspace_id: str
    id: str
    display_name: str
    display_number: int
    title: str
    equipment: str
    case_label: str | None
    source_kind: str
    tags: list[str]
    registration_origin: str
    origin_label: str
    version: int
    facts: list[StoredFact]
    missing_fields: list[str]
    cause_status: CauseStatus


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    workspace_id: str
    id: str
    target_item_id: str | None
    base_version: int
    operations: list[ProposalOperation]
    reason: str
    status: str
    content_hash: str
    equipment: str
    case_label: str | None
    title: str | None
    missing_fields: list[str]
    cause_status: CauseStatus


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    proposal_id: str
    item_id: str
    before_version: int
    after_version: int
    kb_revision: int
    already_applied: bool


@dataclass(frozen=True, slots=True)
class AnswerSnapshotRecord:
    id: str
    stage: str
    action_id: str
    question: str
    question_hash: str
    conversation_id: str
    empty_history: bool
    kb_revision: int
    target_item_id: str | None
    target_version: int | None
    model_id: str
    model_settings: dict[str, Any]
    prompt_version: str
    schema_version: str
    retrieval_version: str
    state: str
    payload: dict[str, Any] | None
    safe_error_code: str | None
    created_at: str


class Repository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.initialize()

    def initialize(self) -> None:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS domain_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS workspaces (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        kb_revision INTEGER NOT NULL DEFAULT 0,
                        seed_mode TEXT NOT NULL,
                        lecture_case_item_id TEXT
                    );
                    CREATE TABLE IF NOT EXISTS sources (
                        workspace_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        external_key TEXT,
                        title TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('document', 'interview')),
                        equipment TEXT NOT NULL,
                        case_label TEXT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, id),
                        UNIQUE (workspace_id, external_key),
                        FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS source_segments (
                        workspace_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        external_key TEXT,
                        source_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        speaker TEXT NOT NULL CHECK (speaker IN ('document', 'operator', 'assistant')),
                        text TEXT NOT NULL,
                        text_sha256 TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, id),
                        UNIQUE (workspace_id, external_key),
                        UNIQUE (workspace_id, source_id, ordinal),
                        FOREIGN KEY (workspace_id, source_id)
                            REFERENCES sources(workspace_id, id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_items (
                        workspace_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        display_number INTEGER NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        equipment TEXT NOT NULL,
                        case_label TEXT,
                        source_kind TEXT NOT NULL DEFAULT 'document',
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        registration_origin TEXT NOT NULL DEFAULT 'user_approved',
                        display_origin_label TEXT NOT NULL DEFAULT 'ユーザー承認',
                        active_version INTEGER NOT NULL,
                        PRIMARY KEY (workspace_id, id),
                        UNIQUE (workspace_id, display_number),
                        FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_versions (
                        workspace_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        facts_json TEXT NOT NULL,
                        missing_fields_json TEXT NOT NULL,
                        cause_status TEXT NOT NULL,
                        approved_at TEXT NOT NULL,
                        approved_by TEXT NOT NULL,
                        proposal_id TEXT,
                        content_hash TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, item_id, version),
                        FOREIGN KEY (workspace_id, item_id)
                            REFERENCES knowledge_items(workspace_id, id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS proposals (
                        workspace_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        target_item_id TEXT,
                        base_version INTEGER NOT NULL,
                        operations_json TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('staged', 'pending', 'approved', 'rejected', 'stale', 'aborted')
                        ),
                        content_hash TEXT NOT NULL,
                        equipment TEXT NOT NULL,
                        case_label TEXT,
                        title TEXT,
                        missing_fields_json TEXT NOT NULL,
                        cause_status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        action_id TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, id),
                        UNIQUE (workspace_id, action_id),
                        FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS approval_events (
                        workspace_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        proposal_id TEXT NOT NULL,
                        decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
                        actor_session_id TEXT NOT NULL,
                        before_version INTEGER NOT NULL,
                        after_version INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, id),
                        UNIQUE (workspace_id, proposal_id),
                        FOREIGN KEY (workspace_id, proposal_id)
                            REFERENCES proposals(workspace_id, id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS conversations (
                        workspace_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, id),
                        FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS conversation_messages (
                        workspace_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                        text TEXT NOT NULL,
                        action_id TEXT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, conversation_id, ordinal),
                        FOREIGN KEY (workspace_id, conversation_id)
                            REFERENCES conversations(workspace_id, id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS conversation_states (
                        workspace_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        state_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, conversation_id),
                        FOREIGN KEY (workspace_id, conversation_id)
                            REFERENCES conversations(workspace_id, id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS answer_snapshots (
                        workspace_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        stage TEXT NOT NULL CHECK (stage IN ('A', 'B', 'C')),
                        action_id TEXT NOT NULL,
                        question TEXT NOT NULL,
                        question_hash TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        empty_history INTEGER NOT NULL CHECK (empty_history IN (0, 1)),
                        kb_revision INTEGER NOT NULL,
                        target_item_id TEXT,
                        target_version INTEGER,
                        model_id TEXT NOT NULL,
                        model_settings_json TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        schema_version TEXT NOT NULL,
                        retrieval_version TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('succeeded', 'failed')),
                        payload_json TEXT,
                        safe_error_code TEXT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, id),
                        UNIQUE (workspace_id, action_id),
                        FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS action_outcomes (
                        workspace_id TEXT NOT NULL,
                        action_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        input_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL,
                        outcome_type TEXT,
                        payload_json TEXT,
                        kb_revision INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, action_id),
                        FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS workspace_guards (
                        workspace_id TEXT PRIMARY KEY,
                        action_id TEXT NOT NULL,
                        claim_token TEXT NOT NULL,
                        kb_revision INTEGER NOT NULL,
                        active INTEGER NOT NULL CHECK (active IN (0, 1)),
                        FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
                    );
                    """
                )
                current = connection.execute(
                    "SELECT value FROM domain_meta WHERE key = 'schema_version'"
                ).fetchone()
                if current is None:
                    connection.execute(
                        "INSERT INTO domain_meta (key, value) VALUES ('schema_version', ?)",
                        (str(DOMAIN_SCHEMA_VERSION),),
                    )
                elif int(current["value"]) == 1:
                    self._migrate_v1_to_v2(connection)
                    connection.execute(
                        "UPDATE domain_meta SET value = ? WHERE key = 'schema_version'",
                        (str(DOMAIN_SCHEMA_VERSION),),
                    )
                elif int(current["value"]) != DOMAIN_SCHEMA_VERSION:
                    raise RuntimeError("unsupported domain schema")
                self._refresh_knowledge_metadata(connection)
        finally:
            connection.close()

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        """Apply only additive columns so existing workspaces and histories remain intact."""

        additions = {
            "workspaces": {
                "lecture_case_item_id": "TEXT",
            },
            "knowledge_items": {
                "title": "TEXT NOT NULL DEFAULT ''",
                "source_kind": "TEXT NOT NULL DEFAULT 'document'",
                "tags_json": "TEXT NOT NULL DEFAULT '[]'",
                "registration_origin": "TEXT NOT NULL DEFAULT 'user_approved'",
                "display_origin_label": "TEXT NOT NULL DEFAULT 'ユーザー承認'",
            },
            "proposals": {
                "title": "TEXT",
            },
        }
        for table, columns in additions.items():
            existing = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, declaration in columns.items():
                if column not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        connection.execute(
            """
            INSERT OR IGNORE INTO conversation_states (
                workspace_id, conversation_id, state_json, updated_at
            )
            SELECT workspace_id, id, '{}', created_at FROM conversations
            """
        )

    def _refresh_knowledge_metadata(self, connection: sqlite3.Connection) -> None:
        """Derive v2 display metadata for both newly and previously migrated databases."""

        active_rows = connection.execute(
            """
            SELECT ki.workspace_id, ki.id, ki.display_number, ki.title,
                   ki.source_kind, kv.facts_json
            FROM knowledge_items ki JOIN knowledge_versions kv
              ON kv.workspace_id = ki.workspace_id AND kv.item_id = ki.id
             AND kv.version = ki.active_version
            """
        ).fetchall()
        for row in active_rows:
            facts = stored_facts_adapter.validate_json(row["facts_json"])
            source_kind = self._source_kind_for_facts(connection, row["workspace_id"], facts)
            title = row["title"] or f"知識項目{row['display_number']}"
            if row["source_kind"] == source_kind and row["title"] == title:
                continue
            connection.execute(
                """
                UPDATE knowledge_items SET source_kind = ?, title = ?
                WHERE workspace_id = ? AND id = ?
                """,
                (source_kind, title, row["workspace_id"], row["id"]),
            )

    def create_workspace(
        self, session_id: str, *, seed_mode: str, seed_path: Path | None = None
    ) -> WorkspaceRecord:
        if seed_mode not in {"from_scratch", "approved_v1", "practical_v5"}:
            raise ValueError("unknown seed mode")
        if seed_mode == "practical_v5" and seed_path is None:
            raise ValidationFailure(
                "初期12件の教材seedが指定されていないため、新規領域を作成しません。",
                code="seed_v5_missing",
            )
        if seed_mode == "from_scratch" and seed_path is not None:
            raise ValidationFailure(
                "空の領域には初期教材を指定できません。",
                code="seed_unexpected",
            )
        current = utc_now()
        workspace_id = str(uuid4())
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                existing = connection.execute(
                    "SELECT * FROM workspaces WHERE session_id = ?", (session_id,)
                ).fetchone()
                if existing is not None:
                    return WorkspaceRecord(
                        existing["id"],
                        existing["session_id"],
                        existing["kb_revision"],
                        existing["seed_mode"],
                        existing["lecture_case_item_id"],
                    )
                connection.execute(
                    """
                    INSERT INTO workspaces (id, session_id, created_at, updated_at, kb_revision, seed_mode)
                    VALUES (?, ?, ?, ?, 0, ?)
                    """,
                    (workspace_id, session_id, current.isoformat(), current.isoformat(), seed_mode),
                )
                if seed_path is not None:
                    self._seed_workspace(connection, workspace_id, seed_path)
                if seed_mode == "approved_v1":
                    self._seed_item3_v1(connection, workspace_id)
                revision = connection.execute(
                    "SELECT kb_revision FROM workspaces WHERE id = ?", (workspace_id,)
                ).fetchone()["kb_revision"]
        finally:
            connection.close()
        return WorkspaceRecord(workspace_id, session_id, revision, seed_mode, None)

    def require_workspace(self, session_id: str, workspace_id: str) -> WorkspaceRecord:
        connection = connect_sqlite(self.path)
        try:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ? AND session_id = ?",
                (workspace_id, session_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise AuthorizationError("指定された作業領域を利用できません。")
        return WorkspaceRecord(
            row["id"],
            row["session_id"],
            row["kb_revision"],
            row["seed_mode"],
            row["lecture_case_item_id"],
        )

    def workspace_for_session(self, session_id: str) -> WorkspaceRecord | None:
        connection = connect_sqlite(self.path)
        try:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE session_id = ?", (session_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return WorkspaceRecord(
            row["id"],
            row["session_id"],
            row["kb_revision"],
            row["seed_mode"],
            row["lecture_case_item_id"],
        )

    def create_conversation(self, workspace_id: str, *, mode: str = "qa") -> str:
        conversation_id = str(uuid4())
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                self._assert_workspace(connection, workspace_id)
                connection.execute(
                    "INSERT INTO conversations (workspace_id, id, mode, created_at) VALUES (?, ?, ?, ?)",
                    (workspace_id, conversation_id, mode, utc_now().isoformat()),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_states (
                        workspace_id, conversation_id, state_json, updated_at
                    ) VALUES (?, ?, '{}', ?)
                    """,
                    (workspace_id, conversation_id, utc_now().isoformat()),
                )
        finally:
            connection.close()
        return conversation_id

    def reset_workspace(
        self,
        session_id: str,
        workspace_id: str,
        *,
        seed_mode: str,
        seed_path: Path | None,
    ) -> tuple[WorkspaceRecord, str]:
        if seed_mode not in {"from_scratch", "approved_v1", "practical_v5"}:
            raise ValueError("unknown seed mode")
        if seed_mode == "practical_v5" and seed_path is None:
            raise ValidationFailure(
                "初期12件の教材seedが指定されていないため、領域を初期化しません。",
                code="seed_v5_missing",
            )
        if seed_mode == "from_scratch" and seed_path is not None:
            raise ValidationFailure(
                "空の領域には初期教材を指定できません。",
                code="seed_unexpected",
            )
        connection = connect_sqlite(self.path)
        conversation_id = str(uuid4())
        try:
            with transaction(connection, immediate=True):
                workspace = connection.execute(
                    "SELECT * FROM workspaces WHERE id = ? AND session_id = ?",
                    (workspace_id, session_id),
                ).fetchone()
                if workspace is None:
                    raise AuthorizationError("作業領域を初期化できません。")
                guard = connection.execute(
                    "SELECT active FROM workspace_guards WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                if guard is not None and guard["active"]:
                    raise ValidationFailure("実行中の操作があるため初期化できません。")
                old_revision = workspace["kb_revision"]
                connection.execute(
                    "DELETE FROM approval_events WHERE workspace_id = ?", (workspace_id,)
                )
                connection.execute("DELETE FROM proposals WHERE workspace_id = ?", (workspace_id,))
                connection.execute(
                    "DELETE FROM knowledge_items WHERE workspace_id = ?", (workspace_id,)
                )
                connection.execute("DELETE FROM sources WHERE workspace_id = ?", (workspace_id,))
                connection.execute(
                    "DELETE FROM conversations WHERE workspace_id = ?", (workspace_id,)
                )
                connection.execute(
                    "DELETE FROM action_outcomes WHERE workspace_id = ?", (workspace_id,)
                )
                connection.execute(
                    "DELETE FROM answer_snapshots WHERE workspace_id = ?", (workspace_id,)
                )
                connection.execute(
                    "DELETE FROM workspace_guards WHERE workspace_id = ?", (workspace_id,)
                )
                if seed_path is not None:
                    self._seed_workspace(connection, workspace_id, seed_path)
                if seed_mode == "approved_v1":
                    self._seed_item3_v1(connection, workspace_id)
                revision = old_revision + 1
                now = utc_now().isoformat()
                connection.execute(
                    """
                    UPDATE workspaces SET kb_revision = ?, seed_mode = ?,
                        lecture_case_item_id = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (revision, seed_mode, now, workspace_id),
                )
                connection.execute(
                    "INSERT INTO conversations (workspace_id, id, mode, created_at) VALUES (?, ?, 'qa', ?)",
                    (workspace_id, conversation_id, now),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_states (
                        workspace_id, conversation_id, state_json, updated_at
                    ) VALUES (?, ?, '{}', ?)
                    """,
                    (workspace_id, conversation_id, now),
                )
        finally:
            connection.close()
        return WorkspaceRecord(workspace_id, session_id, revision, seed_mode, None), conversation_id

    def append_message(
        self,
        workspace_id: str,
        conversation_id: str,
        *,
        role: str,
        text: str,
        action_id: str | None = None,
    ) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("invalid role")
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(ordinal), 0) + 1 AS next_ordinal
                    FROM conversation_messages WHERE workspace_id = ? AND conversation_id = ?
                    """,
                    (workspace_id, conversation_id),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO conversation_messages (
                        workspace_id, conversation_id, ordinal, role, text, action_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        conversation_id,
                        row["next_ordinal"],
                        role,
                        text,
                        action_id,
                        utc_now().isoformat(),
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM conversation_messages
                    WHERE workspace_id = ? AND conversation_id = ? AND ordinal <= (
                        SELECT COALESCE(MAX(ordinal), 0) - 12
                        FROM conversation_messages
                        WHERE workspace_id = ? AND conversation_id = ?
                    )
                    """,
                    (workspace_id, conversation_id, workspace_id, conversation_id),
                )
        finally:
            connection.close()

    def list_messages(self, workspace_id: str, conversation_id: str) -> list[dict[str, Any]]:
        connection = connect_sqlite(self.path)
        try:
            rows = connection.execute(
                """
                SELECT role, text, action_id, created_at FROM conversation_messages
                WHERE workspace_id = ? AND conversation_id = ? ORDER BY ordinal DESC LIMIT 12
                """,
                (workspace_id, conversation_id),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in reversed(rows)]

    def get_conversation_state(self, workspace_id: str, conversation_id: str) -> ConversationState:
        connection = connect_sqlite(self.path)
        try:
            row = connection.execute(
                """
                SELECT state_json FROM conversation_states
                WHERE workspace_id = ? AND conversation_id = ?
                """,
                (workspace_id, conversation_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise AuthorizationError("指定された相談状態を取得できません。")
        try:
            return ConversationState.model_validate_json(row["state_json"])
        except ValidationError as exc:
            raise ValidationFailure("相談状態の形式が破損しています。") from exc

    def save_conversation_state(
        self,
        workspace_id: str,
        conversation_id: str,
        state: ConversationState,
    ) -> None:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                changed = connection.execute(
                    """
                    UPDATE conversation_states SET state_json = ?, updated_at = ?
                    WHERE workspace_id = ? AND conversation_id = ?
                    """,
                    (
                        state.model_dump_json(),
                        utc_now().isoformat(),
                        workspace_id,
                        conversation_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise AuthorizationError("指定された相談状態を更新できません。")
        finally:
            connection.close()

    def register_source(
        self,
        workspace_id: str,
        *,
        title: str,
        kind: str,
        equipment: str,
        case_label: str | None,
        segments: Sequence[tuple[str | None, str, str]],
        external_key: str | None = None,
    ) -> tuple[str, dict[str, str]]:
        if kind not in {"document", "interview"}:
            raise ValueError("invalid source kind")
        source_id = str(uuid4())
        mapping: dict[str, str] = {}
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                self._assert_workspace(connection, workspace_id)
                source_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM sources WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()["count"]
                if source_count >= 30:
                    raise ValidationFailure("この作業領域の原文上限30件に達しました。")
                connection.execute(
                    """
                    INSERT INTO sources (
                        workspace_id, id, external_key, title, kind, equipment, case_label, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        source_id,
                        external_key,
                        title,
                        kind,
                        equipment,
                        case_label,
                        utc_now().isoformat(),
                    ),
                )
                for ordinal, (segment_key, speaker, text) in enumerate(segments, start=1):
                    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
                    segment_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO source_segments (
                            workspace_id, id, external_key, source_id, ordinal, speaker, text, text_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workspace_id,
                            segment_id,
                            segment_key,
                            source_id,
                            ordinal,
                            speaker,
                            normalized,
                            sha256_text(normalized),
                        ),
                    )
                    if segment_key:
                        mapping[segment_key] = segment_id
        finally:
            connection.close()
        return source_id, mapping

    def get_segment_by_external_key(self, workspace_id: str, key: str) -> dict[str, Any]:
        connection = connect_sqlite(self.path)
        try:
            row = connection.execute(
                """
                SELECT ss.*, s.title, s.equipment, s.case_label
                FROM source_segments ss JOIN sources s
                  ON s.workspace_id = ss.workspace_id AND s.id = ss.source_id
                WHERE ss.workspace_id = ? AND ss.external_key = ?
                """,
                (workspace_id, key),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(key)
        return dict(row)

    def list_knowledge(self, workspace_id: str) -> list[KnowledgeRecord]:
        connection = connect_sqlite(self.path)
        try:
            rows = connection.execute(
                """
                SELECT ki.*, kv.version AS selected_version,
                       kv.facts_json, kv.missing_fields_json, kv.cause_status
                FROM knowledge_items ki JOIN knowledge_versions kv
                  ON kv.workspace_id = ki.workspace_id AND kv.item_id = ki.id
                 AND kv.version = ki.active_version
                WHERE ki.workspace_id = ? ORDER BY ki.display_number
                """,
                (workspace_id,),
            ).fetchall()
        finally:
            connection.close()
        return [self._knowledge_from_row(row) for row in rows]

    def get_knowledge(
        self, workspace_id: str, item_id: str, version: int | None = None
    ) -> KnowledgeRecord:
        connection = connect_sqlite(self.path)
        try:
            item = connection.execute(
                "SELECT * FROM knowledge_items WHERE workspace_id = ? AND id = ?",
                (workspace_id, item_id),
            ).fetchone()
            if item is None:
                raise AuthorizationError("指定された知識項目を取得できません。")
            requested = item["active_version"] if version is None else version
            row = connection.execute(
                """
                SELECT ki.*, kv.version AS selected_version,
                       kv.facts_json, kv.missing_fields_json, kv.cause_status
                FROM knowledge_items ki JOIN knowledge_versions kv
                  ON kv.workspace_id = ki.workspace_id AND kv.item_id = ki.id
                WHERE ki.workspace_id = ? AND ki.id = ? AND kv.version = ?
                """,
                (workspace_id, item_id, requested),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise AuthorizationError("指定された知識版を取得できません。")
        record = self._knowledge_from_row(row)
        segment_ids = list(
            dict.fromkeys(ref.segment_id for fact in record.facts for ref in fact.evidence_refs)
        )
        source_kinds = {
            str(segment["kind"]) for segment in self.read_segments(workspace_id, segment_ids)
        }
        if source_kinds == {"document"}:
            version_source_kind = "document"
        elif source_kinds == {"interview"}:
            version_source_kind = "interview"
        elif source_kinds == {"document", "interview"}:
            version_source_kind = "mixed"
        else:
            raise ValidationFailure("知識版の根拠由来を判定できません。")
        return replace(record, source_kind=version_source_kind)

    def allowed_evidence_ids(self, workspace_id: str) -> set[str]:
        allowed: set[str] = set()
        for item in self.list_knowledge(workspace_id):
            for fact in item.facts:
                allowed.update(ref.segment_id for ref in fact.evidence_refs)
        return allowed

    def read_segments(self, workspace_id: str, segment_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(segment_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        connection = connect_sqlite(self.path)
        try:
            rows = connection.execute(
                f"""
                SELECT ss.id, ss.ordinal, ss.speaker, ss.text, ss.text_sha256,
                       s.id AS source_id, s.title, s.kind, s.equipment, s.case_label
                FROM source_segments ss JOIN sources s
                  ON s.workspace_id = ss.workspace_id AND s.id = ss.source_id
                WHERE ss.workspace_id = ? AND ss.id IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated from list length only
                [workspace_id, *ids],
            ).fetchall()
        finally:
            connection.close()
        by_id = {row["id"]: dict(row) for row in rows}
        if set(by_id) != set(ids):
            raise AuthorizationError("取得できない根拠が含まれています。")
        return [by_id[item] for item in ids]

    def read_source_transcripts(
        self, workspace_id: str, evidence_segment_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        """Read full source transcripts anchored by known evidence segments.

        This is a read-only UI operation. Agent evidence access continues to return only
        approved evidence IDs and never broadens from an answer to an assistant question.
        """

        ids = list(dict.fromkeys(evidence_segment_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        connection = connect_sqlite(self.path)
        try:
            anchor_rows = connection.execute(
                f"""
                SELECT id, source_id FROM source_segments
                WHERE workspace_id = ? AND id IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated from list length only
                [workspace_id, *ids],
            ).fetchall()
            if {str(row["id"]) for row in anchor_rows} != set(ids):
                raise AuthorizationError("取得できない根拠が含まれています。")
            source_ids = list(dict.fromkeys(str(row["source_id"]) for row in anchor_rows))
            source_placeholders = ",".join("?" for _ in source_ids)
            rows = connection.execute(
                f"""
                SELECT ss.id, ss.ordinal, ss.speaker, ss.text, ss.text_sha256,
                       s.id AS source_id, s.title, s.kind, s.equipment, s.case_label
                FROM source_segments ss JOIN sources s
                  ON s.workspace_id = ss.workspace_id AND s.id = ss.source_id
                WHERE ss.workspace_id = ? AND ss.source_id IN ({source_placeholders})
                ORDER BY s.created_at, ss.ordinal
                """,  # noqa: S608 - placeholders are generated from validated source ids
                [workspace_id, *source_ids],
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def stage_proposal(
        self,
        workspace_id: str,
        *,
        action_id: str,
        target_item_id: str | None,
        base_version: int,
        operations: list[ProposalOperation],
        reason: str,
        equipment: str,
        case_label: str | None,
        missing_fields: list[str],
        cause_status: CauseStatus,
        allowed_segment_ids: set[str],
        title: str | None = None,
    ) -> ProposalRecord:
        if not operations:
            raise ValidationFailure("変更内容が空です。")
        payload = {
            "target_item_id": target_item_id,
            "base_version": base_version,
            "operations": [op.model_dump(mode="json") for op in operations],
            "reason": reason,
            "equipment": equipment,
            "case_label": case_label,
            "title": title,
            "missing_fields": missing_fields,
            "cause_status": cause_status.value,
        }
        content_hash = sha256_text(canonical_json(payload))
        connection = connect_sqlite(self.path)
        proposal_id = str(uuid4())
        try:
            with transaction(connection, immediate=True):
                self._assert_workspace(connection, workspace_id)
                existing = connection.execute(
                    "SELECT * FROM proposals WHERE workspace_id = ? AND action_id = ?",
                    (workspace_id, action_id),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] != content_hash:
                        raise ValidationFailure(
                            "同じ操作IDに異なる更新案を作成できません。",
                            code="proposal_action_conflict",
                        )
                    return self._proposal_from_row(existing)
                pending_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM proposals
                    WHERE workspace_id = ? AND status IN ('staged', 'pending')
                    """,
                    (workspace_id,),
                ).fetchone()["count"]
                if pending_count >= 20:
                    raise ValidationFailure("この作業領域の未処理更新案上限20件に達しました。")
                if target_item_id is None:
                    if base_version != 0:
                        raise ValidationFailure("新規知識のbase_versionは0です。")
                    knowledge_count = connection.execute(
                        "SELECT COUNT(*) AS count FROM knowledge_items WHERE workspace_id = ?",
                        (workspace_id,),
                    ).fetchone()["count"]
                    if knowledge_count >= 30:
                        raise ValidationFailure("この作業領域の知識上限30件に達しました。")
                else:
                    item = connection.execute(
                        """
                        SELECT active_version, equipment, case_label FROM knowledge_items
                        WHERE workspace_id = ? AND id = ?
                        """,
                        (workspace_id, target_item_id),
                    ).fetchone()
                    if item is None:
                        raise AuthorizationError("更新対象を取得できません。")
                    if item["active_version"] != base_version:
                        raise ValidationFailure("更新対象の版が変わっています。")
                    if item["equipment"] != equipment or item["case_label"] != case_label:
                        raise ValidationFailure("更新案で設備・事例を変更できません。")
                for operation in operations:
                    if operation.new_fact is not None:
                        self._validate_fact_evidence(
                            connection,
                            workspace_id,
                            operation.new_fact,
                            allowed_segment_ids,
                        )
                connection.execute(
                    """
                    INSERT INTO proposals (
                        workspace_id, id, target_item_id, base_version, operations_json,
                        reason, status, content_hash, equipment, case_label,
                        title, missing_fields_json, cause_status, created_at, action_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        proposal_id,
                        target_item_id,
                        base_version,
                        canonical_json(payload["operations"]),
                        reason,
                        content_hash,
                        equipment,
                        case_label,
                        title,
                        canonical_json(missing_fields),
                        cause_status.value,
                        utc_now().isoformat(),
                        action_id,
                    ),
                )
        finally:
            connection.close()
        return self.get_proposal(workspace_id, proposal_id)

    def publish_proposal(self, workspace_id: str, proposal_id: str) -> ProposalRecord:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                changed = connection.execute(
                    """
                    UPDATE proposals SET status = 'pending'
                    WHERE workspace_id = ? AND id = ? AND status = 'staged'
                    """,
                    (workspace_id, proposal_id),
                ).rowcount
                if changed != 1:
                    raise ValidationFailure("更新案をレビュー対象として確定できません。")
        finally:
            connection.close()
        return self.get_proposal(workspace_id, proposal_id)

    def abort_staged_proposal(self, workspace_id: str, proposal_id: str) -> None:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                connection.execute(
                    """
                    UPDATE proposals SET status = 'aborted'
                    WHERE workspace_id = ? AND id = ? AND status = 'staged'
                    """,
                    (workspace_id, proposal_id),
                )
        finally:
            connection.close()

    def get_proposal(self, workspace_id: str, proposal_id: str) -> ProposalRecord:
        connection = connect_sqlite(self.path)
        try:
            row = connection.execute(
                "SELECT * FROM proposals WHERE workspace_id = ? AND id = ?",
                (workspace_id, proposal_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise AuthorizationError("指定された更新案を取得できません。")
        return self._proposal_from_row(row)

    def list_pending_proposals(self, workspace_id: str) -> list[ProposalRecord]:
        connection = connect_sqlite(self.path)
        try:
            rows = connection.execute(
                """
                SELECT * FROM proposals WHERE workspace_id = ? AND status = 'pending'
                ORDER BY created_at
                """,
                (workspace_id,),
            ).fetchall()
        finally:
            connection.close()
        return [self._proposal_from_row(row) for row in rows]

    def approve_proposal(
        self,
        workspace_id: str,
        proposal_id: str,
        *,
        actor_session_id: str,
        expected_content_hash: str,
    ) -> ApprovalResult:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                workspace = self._assert_workspace(connection, workspace_id)
                proposal = connection.execute(
                    "SELECT * FROM proposals WHERE workspace_id = ? AND id = ?",
                    (workspace_id, proposal_id),
                ).fetchone()
                if proposal is None:
                    raise AuthorizationError("指定された更新案を取得できません。")
                existing_event = connection.execute(
                    """
                    SELECT * FROM approval_events
                    WHERE workspace_id = ? AND proposal_id = ? AND decision = 'approved'
                    """,
                    (workspace_id, proposal_id),
                ).fetchone()
                if existing_event is not None:
                    return ApprovalResult(
                        proposal_id,
                        proposal["target_item_id"]
                        or self._item_for_proposal(connection, workspace_id, proposal_id),
                        existing_event["before_version"],
                        existing_event["after_version"],
                        workspace["kb_revision"],
                        True,
                    )
                if proposal["status"] != "pending":
                    raise ValidationFailure("pending状態の更新案だけを承認できます。")
                if proposal["content_hash"] != expected_content_hash:
                    raise ValidationFailure("表示後に更新案が変化したため承認しません。")
                operations = operations_adapter.validate_json(proposal["operations_json"])
                target_id = proposal["target_item_id"]
                if target_id is None:
                    if proposal["base_version"] != 0:
                        raise ValidationFailure("新規知識の版が不正です。")
                    target_id = str(uuid4())
                    display_number = connection.execute(
                        """
                        SELECT COALESCE(MAX(display_number), 0) + 1 AS next_number
                        FROM knowledge_items WHERE workspace_id = ?
                        """,
                        (workspace_id,),
                    ).fetchone()["next_number"]
                    connection.execute(
                        """
                        INSERT INTO knowledge_items (
                            workspace_id, id, display_number, title, equipment, case_label,
                            source_kind, tags_json, registration_origin,
                            display_origin_label, active_version
                        ) VALUES (?, ?, ?, ?, ?, ?, 'document', '[]',
                                  'user_approved', '利用者が承認', 0)
                        """,
                        (
                            workspace_id,
                            target_id,
                            display_number,
                            proposal["title"] or f"{proposal['equipment']}の知識",
                            proposal["equipment"],
                            proposal["case_label"],
                        ),
                    )
                    before_version = 0
                    facts: list[StoredFact] = []
                else:
                    item = connection.execute(
                        "SELECT * FROM knowledge_items WHERE workspace_id = ? AND id = ?",
                        (workspace_id, target_id),
                    ).fetchone()
                    if item is None:
                        raise AuthorizationError("更新対象を取得できません。")
                    before_version = item["active_version"]
                    if before_version != proposal["base_version"]:
                        raise _StaleProposalError
                    version_row = connection.execute(
                        """
                        SELECT facts_json FROM knowledge_versions
                        WHERE workspace_id = ? AND item_id = ? AND version = ?
                        """,
                        (workspace_id, target_id, before_version),
                    ).fetchone()
                    facts = stored_facts_adapter.validate_json(version_row["facts_json"])
                allowed_segments = self._all_workspace_segment_ids(connection, workspace_id)
                facts = self._apply_operations(
                    connection, workspace_id, facts, operations, allowed_segments
                )
                source_kind = self._source_kind_for_facts(connection, workspace_id, facts)
                after_version = before_version + 1
                facts_payload = [fact.model_dump(mode="json") for fact in facts]
                version_payload = {
                    "facts": facts_payload,
                    "missing_fields": json.loads(proposal["missing_fields_json"]),
                    "cause_status": proposal["cause_status"],
                }
                connection.execute(
                    """
                    INSERT INTO knowledge_versions (
                        workspace_id, item_id, version, facts_json, missing_fields_json,
                        cause_status, approved_at, approved_by, proposal_id, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        target_id,
                        after_version,
                        canonical_json(facts_payload),
                        proposal["missing_fields_json"],
                        proposal["cause_status"],
                        utc_now().isoformat(),
                        actor_session_id,
                        proposal_id,
                        sha256_text(canonical_json(version_payload)),
                    ),
                )
                connection.execute(
                    """
                    UPDATE knowledge_items SET active_version = ?, source_kind = ?
                    WHERE workspace_id = ? AND id = ?
                    """,
                    (after_version, source_kind, workspace_id, target_id),
                )
                event_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO approval_events (
                        workspace_id, id, proposal_id, decision, actor_session_id,
                        before_version, after_version, created_at
                    ) VALUES (?, ?, ?, 'approved', ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        event_id,
                        proposal_id,
                        actor_session_id,
                        before_version,
                        after_version,
                        utc_now().isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE proposals SET status = 'approved', target_item_id = ? WHERE workspace_id = ? AND id = ?",
                    (target_id, workspace_id, proposal_id),
                )
                revision = workspace["kb_revision"] + 1
                if (
                    before_version == 0
                    and workspace["seed_mode"] == "practical_v5"
                    and workspace["lecture_case_item_id"] is None
                ):
                    connection.execute(
                        "UPDATE workspaces SET lecture_case_item_id = ? WHERE id = ?",
                        (target_id, workspace_id),
                    )
                connection.execute(
                    """
                    UPDATE workspaces SET kb_revision = ?, updated_at = ? WHERE id = ?
                    """,
                    (revision, utc_now().isoformat(), workspace_id),
                )
                return ApprovalResult(
                    proposal_id, target_id, before_version, after_version, revision, False
                )
        except _StaleProposalError:
            with transaction(connection, immediate=True):
                connection.execute(
                    """
                    UPDATE proposals SET status = 'stale'
                    WHERE workspace_id = ? AND id = ? AND status = 'pending'
                    """,
                    (workspace_id, proposal_id),
                )
            raise ValidationFailure(
                "現行版が進んだため、この更新案はstaleです。",
                code="proposal_stale",
            ) from None
        except sqlite3.IntegrityError as exc:
            raise ValidationFailure("承認処理の整合性検査に失敗しました。") from exc
        finally:
            connection.close()

    def reject_proposal(
        self, workspace_id: str, proposal_id: str, *, actor_session_id: str
    ) -> None:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                proposal = connection.execute(
                    "SELECT * FROM proposals WHERE workspace_id = ? AND id = ?",
                    (workspace_id, proposal_id),
                ).fetchone()
                if proposal is None or proposal["status"] != "pending":
                    raise ValidationFailure("pending状態の更新案だけを却下できます。")
                connection.execute(
                    "UPDATE proposals SET status = 'rejected' WHERE workspace_id = ? AND id = ?",
                    (workspace_id, proposal_id),
                )
                connection.execute(
                    """
                    INSERT INTO approval_events (
                        workspace_id, id, proposal_id, decision, actor_session_id,
                        before_version, after_version, created_at
                    ) VALUES (?, ?, ?, 'rejected', ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        str(uuid4()),
                        proposal_id,
                        actor_session_id,
                        proposal["base_version"],
                        proposal["base_version"],
                        utc_now().isoformat(),
                    ),
                )
        finally:
            connection.close()

    def begin_action_guard(
        self,
        workspace_id: str,
        *,
        action_id: str,
        claim_token: str,
        kb_revision: int,
    ) -> None:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                workspace = self._assert_workspace(connection, workspace_id)
                if workspace["kb_revision"] != kb_revision:
                    raise StaleContextError()
                existing = connection.execute(
                    "SELECT * FROM workspace_guards WHERE workspace_id = ?", (workspace_id,)
                ).fetchone()
                if existing is not None and existing["active"]:
                    raise AppError(
                        "workspace_busy", "この作業領域では別の操作が実行中です。", "job_guard"
                    )
                connection.execute(
                    """
                    INSERT INTO workspace_guards (
                        workspace_id, action_id, claim_token, kb_revision, active
                    ) VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(workspace_id) DO UPDATE SET
                        action_id = excluded.action_id,
                        claim_token = excluded.claim_token,
                        kb_revision = excluded.kb_revision,
                        active = 1
                    """,
                    (workspace_id, action_id, claim_token, kb_revision),
                )
        finally:
            connection.close()

    def invalidate_action_guard(self, workspace_id: str, action_id: str) -> bool:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                changed = connection.execute(
                    """
                    UPDATE workspace_guards SET active = 0
                    WHERE workspace_id = ? AND action_id = ? AND active = 1
                    """,
                    (workspace_id, action_id),
                ).rowcount
                return changed == 1
        finally:
            connection.close()

    def save_action_outcome(
        self,
        workspace_id: str,
        *,
        action_id: str,
        session_id: str,
        claim_token: str,
        input_sha256: str,
        kb_revision: int,
        outcome_type: str,
        payload: dict[str, Any],
    ) -> str:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                existing = connection.execute(
                    """
                    SELECT * FROM action_outcomes
                    WHERE workspace_id = ? AND action_id = ?
                    """,
                    (workspace_id, action_id),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["session_id"] == session_id
                        and existing["input_sha256"] == input_sha256
                        and existing["kb_revision"] == kb_revision
                    ):
                        return str(existing["action_id"])
                    raise ValidationFailure("同じ操作IDに異なる結果を保存できません。")
                guard = connection.execute(
                    "SELECT * FROM workspace_guards WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                if (
                    guard is None
                    or not guard["active"]
                    or guard["action_id"] != action_id
                    or guard["claim_token"] != claim_token
                ):
                    raise AppError(
                        "job_cancelled_before_commit",
                        "取消または競合により成果を公開しません。",
                        "outcome_commit",
                    )
                workspace = self._assert_workspace(connection, workspace_id)
                if workspace["kb_revision"] != kb_revision:
                    connection.execute(
                        "UPDATE workspace_guards SET active = 0 WHERE workspace_id = ?",
                        (workspace_id,),
                    )
                    raise StaleContextError()
                connection.execute(
                    """
                    INSERT INTO action_outcomes (
                        workspace_id, action_id, session_id, input_sha256, state,
                        outcome_type, payload_json, kb_revision, created_at
                    ) VALUES (?, ?, ?, ?, 'succeeded', ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        action_id,
                        session_id,
                        input_sha256,
                        outcome_type,
                        canonical_json(payload),
                        kb_revision,
                        utc_now().isoformat(),
                    ),
                )
                if outcome_type == "proposal":
                    proposal_id = payload.get("proposal_id")
                    if not isinstance(proposal_id, str):
                        raise ValidationFailure("提案成果にproposal_idがありません。")
                    published = connection.execute(
                        """
                        UPDATE proposals SET status = 'pending'
                        WHERE workspace_id = ? AND id = ? AND action_id = ? AND status = 'staged'
                        """,
                        (workspace_id, proposal_id, action_id),
                    ).rowcount
                    if published != 1:
                        raise ValidationFailure("検証済み提案をpendingへ確定できません。")
                elif outcome_type == "answer":
                    selection = payload.get("selection")
                    if not isinstance(selection, dict) or not isinstance(
                        selection.get("status"), str
                    ):
                        raise ValidationFailure("回答成果の形式が不正です。")
                    ordinal = connection.execute(
                        """
                        SELECT COALESCE(MAX(ordinal), 0) + 1 AS next_ordinal
                        FROM conversation_messages
                        WHERE workspace_id = ? AND conversation_id = ?
                        """,
                        (workspace_id, str(payload.get("conversation_id"))),
                    ).fetchone()["next_ordinal"]
                    connection.execute(
                        """
                        INSERT INTO conversation_messages (
                            workspace_id, conversation_id, ordinal, role, text, action_id, created_at
                        ) VALUES (?, ?, ?, 'assistant', ?, ?, ?)
                        """,
                        (
                            workspace_id,
                            str(payload.get("conversation_id")),
                            ordinal,
                            canonical_json(
                                {
                                    "status": selection["status"],
                                    "candidate_count": len(selection.get("candidates", [])),
                                }
                            ),
                            action_id,
                            utc_now().isoformat(),
                        ),
                    )
                    state_row = connection.execute(
                        """
                        SELECT state_json FROM conversation_states
                        WHERE workspace_id = ? AND conversation_id = ?
                        """,
                        (workspace_id, str(payload.get("conversation_id"))),
                    ).fetchone()
                    if state_row is not None:
                        state = ConversationState.model_validate_json(state_row["state_json"])
                        candidate_ids = [
                            str(candidate.get("knowledge_id"))
                            for candidate in selection.get("candidates", [])
                            if isinstance(candidate, dict)
                            and isinstance(candidate.get("knowledge_id"), str)
                        ]
                        state = state.model_copy(
                            update={
                                "focus_answer_id": action_id,
                                "focus_knowledge_ids": candidate_ids[:3],
                                "reference_is_ambiguous": False,
                            }
                        )
                        connection.execute(
                            """
                            UPDATE conversation_states SET state_json = ?, updated_at = ?
                            WHERE workspace_id = ? AND conversation_id = ?
                            """,
                            (
                                state.model_dump_json(),
                                utc_now().isoformat(),
                                workspace_id,
                                str(payload.get("conversation_id")),
                            ),
                        )
                    comparison = payload.get("comparison")
                    if comparison is not None:
                        if not isinstance(comparison, dict):
                            raise ValidationFailure("比較記録の形式が不正です。")
                        self._insert_answer_snapshot(
                            connection,
                            workspace_id=workspace_id,
                            action_id=action_id,
                            state="succeeded",
                            comparison=comparison,
                            payload=payload,
                            safe_error_code=None,
                        )
                connection.execute(
                    "UPDATE workspace_guards SET active = 0 WHERE workspace_id = ?",
                    (workspace_id,),
                )
                return action_id
        finally:
            connection.close()

    def get_action_outcome(
        self, workspace_id: str, action_id: str, *, session_id: str
    ) -> dict[str, Any] | None:
        connection = connect_sqlite(self.path)
        try:
            row = connection.execute(
                """
                SELECT action_id, input_sha256, state, outcome_type, payload_json,
                       kb_revision, created_at
                FROM action_outcomes
                WHERE workspace_id = ? AND action_id = ? AND session_id = ?
                """,
                (workspace_id, action_id, session_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def list_answer_snapshots(self, workspace_id: str) -> list[AnswerSnapshotRecord]:
        connection = connect_sqlite(self.path)
        try:
            rows = connection.execute(
                """
                SELECT * FROM answer_snapshots
                WHERE workspace_id = ? ORDER BY created_at, id
                """,
                (workspace_id,),
            ).fetchall()
        finally:
            connection.close()
        return [self._answer_snapshot_from_row(row) for row in rows]

    def record_answer_snapshot_failure(
        self,
        workspace_id: str,
        *,
        action_id: str,
        comparison: dict[str, Any],
        safe_error_code: str,
    ) -> None:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                self._assert_workspace(connection, workspace_id)
                self._insert_answer_snapshot(
                    connection,
                    workspace_id=workspace_id,
                    action_id=action_id,
                    state="failed",
                    comparison=comparison,
                    payload=None,
                    safe_error_code=safe_error_code,
                )
        finally:
            connection.close()

    def knowledge_stats(self, workspace_id: str) -> dict[str, object]:
        items = self.list_knowledge(workspace_id)
        return {
            "knowledge_count": len(items),
            "source_counts": dict(Counter(item.source_kind for item in items)),
            "equipment_count": len({item.equipment for item in items}),
            "equipment_counts": dict(Counter(item.equipment for item in items)),
        }

    def abort_staged_by_action(self, workspace_id: str, action_id: str) -> None:
        connection = connect_sqlite(self.path)
        try:
            with transaction(connection, immediate=True):
                connection.execute(
                    """
                    UPDATE proposals SET status = 'aborted'
                    WHERE workspace_id = ? AND action_id = ? AND status = 'staged'
                    """,
                    (workspace_id, action_id),
                )
        finally:
            connection.close()

    def _seed_workspace(
        self, connection: sqlite3.Connection, workspace_id: str, path: Path
    ) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationFailure(
                "初期教材seedを読み取れないため、新規領域を作成しません。",
                code="seed_invalid",
            ) from exc
        if payload.get("fixture_schema") == "wg4-seed-manifest-v5-proposal":
            self._seed_v5_workspace(connection, workspace_id, load_v5_seed(path))
            return
        segment_ids: dict[str, str] = {}
        segment_texts: dict[str, str] = {}
        for source in payload["approved_sources"]:
            source_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO sources (
                    workspace_id, id, external_key, title, kind, equipment, case_label, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    workspace_id,
                    source_id,
                    source["key"],
                    source["title"],
                    source["kind"],
                    source["equipment"],
                    utc_now().isoformat(),
                ),
            )
            for ordinal, segment in enumerate(source["segments"], start=1):
                segment_id = str(uuid4())
                text = segment["text"]
                segment_ids[segment["key"]] = segment_id
                segment_texts[segment["key"]] = text
                connection.execute(
                    """
                    INSERT INTO source_segments (
                        workspace_id, id, external_key, source_id, ordinal, speaker, text, text_sha256
                    ) VALUES (?, ?, ?, ?, ?, 'document', ?, ?)
                    """,
                    (
                        workspace_id,
                        segment_id,
                        segment["key"],
                        source_id,
                        ordinal,
                        text,
                        sha256_text(text),
                    ),
                )
        for display_number, card in enumerate(payload["seed_cards"], start=1):
            facts: list[StoredFact] = []
            action_id = str(uuid4())
            facts.append(
                StoredFact(
                    id=action_id,
                    kind=FactKind.CHECK_ACTION,
                    text=card["action"],
                    evidence_refs=[
                        self._seed_evidence(segment_ids[key], segment_texts[key], card["action"])
                        for key in card["action_evidence"]
                    ],
                )
            )
            for condition in card["case_conditions"]:
                facts.append(
                    StoredFact(
                        id=str(uuid4()),
                        kind=FactKind.CONDITION,
                        text=condition["text"],
                        condition_scope=ConditionScope.CASE_CONTEXT,
                        evidence_refs=[
                            self._seed_evidence(
                                segment_ids[key], segment_texts[key], condition["text"]
                            )
                            for key in condition["evidence"]
                        ],
                    )
                )
            facts.append(
                StoredFact(
                    id=str(uuid4()),
                    kind=FactKind.CAUSE_STATUS,
                    text="原因は未確定",
                    evidence_refs=[
                        self._seed_evidence(segment_ids[key], segment_texts[key], None)
                        for key in card["cause_status_evidence"]
                    ],
                )
            )
            item_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO knowledge_items (
                    workspace_id, id, display_number, title, equipment, case_label,
                    source_kind, tags_json, registration_origin,
                    display_origin_label, active_version
                ) VALUES (?, ?, ?, ?, ?, ?, 'document', '[]',
                          'synthetic_fixture', '初期収録（架空教材）', 1)
                """,
                (
                    workspace_id,
                    item_id,
                    display_number,
                    card["display_name"],
                    card["equipment"],
                    card["case_label"],
                ),
            )
            self._insert_fixture_version(
                connection,
                workspace_id,
                item_id,
                facts,
                CauseStatus(card["cause_status"]),
                missing_fields=[],
            )
        connection.execute(
            "UPDATE workspaces SET kb_revision = 1, updated_at = ? WHERE id = ?",
            (utc_now().isoformat(), workspace_id),
        )

    def _seed_v5_workspace(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        manifest: V5SeedManifest,
    ) -> None:
        segment_ids: dict[str, str] = {}
        segment_texts: dict[str, str] = {}
        for source in manifest.sources:
            source_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO sources (
                    workspace_id, id, external_key, title, kind, equipment, case_label, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    source_id,
                    source.key,
                    source.title,
                    source.kind,
                    source.equipment_label,
                    source.case_label,
                    utc_now().isoformat(),
                ),
            )
            for ordinal, segment in enumerate(source.segments, start=1):
                segment_id = str(uuid4())
                normalized = segment.text.replace("\r\n", "\n").replace("\r", "\n")
                segment_ids[segment.key] = segment_id
                segment_texts[segment.key] = normalized
                connection.execute(
                    """
                    INSERT INTO source_segments (
                        workspace_id, id, external_key, source_id, ordinal,
                        speaker, text, text_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id,
                        segment_id,
                        segment.key,
                        source_id,
                        ordinal,
                        segment.speaker,
                        normalized,
                        sha256_text(normalized),
                    ),
                )

        for item in manifest.knowledge_items:
            facts: list[StoredFact] = []
            for fact in item.facts:
                refs: list[EvidenceRef] = []
                for evidence in fact.evidence:
                    source_text = segment_texts[evidence.segment_key]
                    start = source_text.index(evidence.quote)
                    refs.append(
                        EvidenceRef(
                            segment_id=segment_ids[evidence.segment_key],
                            quote=evidence.quote,
                            start_char=start,
                            end_char=start + len(evidence.quote),
                        )
                    )
                facts.append(
                    StoredFact(
                        id=str(uuid4()),
                        kind=fact.kind,
                        text=fact.text,
                        condition_scope=fact.condition_scope,
                        evidence_refs=refs,
                    )
                )
            item_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO knowledge_items (
                    workspace_id, id, display_number, title, equipment, case_label,
                    source_kind, tags_json, registration_origin,
                    display_origin_label, active_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    workspace_id,
                    item_id,
                    item.display_number,
                    item.title,
                    item.equipment_label,
                    item.case_label,
                    item.source_kind,
                    canonical_json(item.tags),
                    item.registration_origin,
                    manifest.import_contract.status_label,
                ),
            )
            self._insert_fixture_version(
                connection,
                workspace_id,
                item_id,
                facts,
                item.cause_status,
                missing_fields=item.missing_fields,
                approved_by=manifest.import_contract.seed_actor,
            )
        connection.execute(
            "UPDATE workspaces SET kb_revision = 1, updated_at = ? WHERE id = ?",
            (utc_now().isoformat(), workspace_id),
        )

    def _seed_item3_v1(self, connection: sqlite3.Connection, workspace_id: str) -> None:
        source_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO sources (
                workspace_id, id, external_key, title, kind, equipment, case_label, created_at
            ) VALUES (?, ?, 'prepared-item3', '確認済み知識項目3 v1', 'interview',
                      '冷却器1', '事例1', ?)
            """,
            (workspace_id, source_id, utc_now().isoformat()),
        )
        texts = [
            (
                "prepared-item3-p1",
                "冷却器1では、温度計を交換してから出口温度が高めに表示されるように"
                "なったため、念のため別の計器とも突き合わせた。",
            ),
            ("prepared-item3-p2", "ただし、表示差の原因までは特定できていない。"),
            ("prepared-item3-p3", "冷却水の流量と入口温度は、どちらも通常の範囲でした。"),
        ]
        segment_map: dict[str, tuple[str, str]] = {}
        for ordinal, (key, text) in enumerate(texts, start=1):
            segment_id = str(uuid4())
            segment_map[key] = (segment_id, text)
            connection.execute(
                """
                INSERT INTO source_segments (
                    workspace_id, id, external_key, source_id, ordinal, speaker, text, text_sha256
                ) VALUES (?, ?, ?, ?, ?, 'operator', ?, ?)
                """,
                (workspace_id, segment_id, key, source_id, ordinal, text, sha256_text(text)),
            )
        action_id = str(uuid4())
        fact_specs = [
            (
                FactKind.OBSERVATION,
                "出口温度の表示上昇",
                ConditionScope.CASE_CONTEXT,
                "prepared-item3-p1",
            ),
            (
                FactKind.CONDITION,
                "温度計の交換直後",
                ConditionScope.CASE_CONTEXT,
                "prepared-item3-p1",
            ),
            (FactKind.CHECK_ACTION, "別計器との照合", None, "prepared-item3-p1"),
            (
                FactKind.CONDITION,
                "冷却水流量と入口温度は通常範囲",
                ConditionScope.CASE_CONTEXT,
                "prepared-item3-p3",
            ),
            (FactKind.CAUSE_STATUS, "原因は未確定", None, "prepared-item3-p2"),
        ]
        facts: list[StoredFact] = []
        for kind, text, scope, key in fact_specs:
            segment_id, source_text = segment_map[key]
            facts.append(
                StoredFact(
                    id=action_id if kind is FactKind.CHECK_ACTION else str(uuid4()),
                    kind=kind,
                    text=text,
                    condition_scope=scope if kind is FactKind.CONDITION else None,
                    evidence_refs=[self._seed_evidence(segment_id, source_text, None)],
                )
            )
        item_id = str(uuid4())
        next_number = connection.execute(
            "SELECT COALESCE(MAX(display_number), 0) + 1 AS n FROM knowledge_items WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()["n"]
        connection.execute(
            """
            INSERT INTO knowledge_items (
                workspace_id, id, display_number, title, equipment, case_label,
                source_kind, registration_origin, display_origin_label, active_version
            ) VALUES (?, ?, ?, '温度計交換後の出口温度表示', '冷却器1', '事例1',
                      'interview', 'synthetic_fixture', '初期収録（架空教材）', 1)
            """,
            (workspace_id, item_id, next_number),
        )
        self._insert_fixture_version(
            connection,
            workspace_id,
            item_id,
            facts,
            CauseStatus.UNRESOLVED,
            missing_fields=["判断理由", "照合結果", "例外・一般化可能な範囲"],
        )
        connection.execute(
            "UPDATE workspaces SET kb_revision = kb_revision + 1, updated_at = ? WHERE id = ?",
            (utc_now().isoformat(), workspace_id),
        )

    def _insert_fixture_version(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        item_id: str,
        facts: list[StoredFact],
        cause_status: CauseStatus,
        *,
        missing_fields: list[str],
        approved_by: str = "fixture_author",
    ) -> None:
        facts_json = canonical_json([fact.model_dump(mode="json") for fact in facts])
        version_payload = {
            "facts": json.loads(facts_json),
            "missing_fields": missing_fields,
            "cause_status": cause_status.value,
        }
        connection.execute(
            """
            INSERT INTO knowledge_versions (
                workspace_id, item_id, version, facts_json, missing_fields_json,
                cause_status, approved_at, approved_by, proposal_id, content_hash
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                workspace_id,
                item_id,
                facts_json,
                canonical_json(missing_fields),
                cause_status.value,
                utc_now().isoformat(),
                approved_by,
                sha256_text(canonical_json(version_payload)),
            ),
        )

    def _seed_evidence(
        self, segment_id: str, source_text: str, preferred_quote: str | None
    ) -> EvidenceRef:
        quote = (
            preferred_quote if preferred_quote and preferred_quote in source_text else source_text
        )
        start = source_text.index(quote)
        return EvidenceRef(
            segment_id=segment_id, quote=quote, start_char=start, end_char=start + len(quote)
        )

    def _apply_operations(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        facts: list[StoredFact],
        operations: list[ProposalOperation],
        allowed_segment_ids: set[str],
    ) -> list[StoredFact]:
        output = list(facts)
        for operation in operations:
            index = next(
                (i for i, fact in enumerate(output) if fact.id == operation.target_fact_id), None
            )
            if operation.operation is OperationType.ADD_FACT:
                assert operation.new_fact is not None
                output.append(
                    self._stored_fact_from_draft(
                        connection, workspace_id, operation.new_fact, allowed_segment_ids
                    )
                )
            elif operation.operation is OperationType.REPLACE_FACT:
                if index is None or operation.new_fact is None:
                    raise ValidationFailure("置換対象のfactが現行版にありません。")
                replacement = self._stored_fact_from_draft(
                    connection,
                    workspace_id,
                    operation.new_fact,
                    allowed_segment_ids,
                    fact_id=operation.target_fact_id,
                )
                output[index] = replacement
            else:
                if index is None:
                    raise ValidationFailure("削除対象のfactが現行版にありません。")
                output.pop(index)
        if not any(fact.kind is FactKind.CHECK_ACTION for fact in output):
            raise ValidationFailure("知識項目には確認行動が必要です。")
        known_ids = {fact.id for fact in output}
        for fact in output:
            if (
                fact.condition_scope is ConditionScope.ACTION_PREREQUISITE
                and fact.parent_action_fact_id not in known_ids
            ):
                raise ValidationFailure("行動前提の参照先がありません。")
        return output

    def _stored_fact_from_draft(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        draft: FactDraft,
        allowed_segment_ids: set[str],
        *,
        fact_id: str | None = None,
    ) -> StoredFact:
        self._validate_fact_evidence(connection, workspace_id, draft, allowed_segment_ids)
        refs = [self._resolve_evidence(connection, workspace_id, item) for item in draft.evidence]
        return StoredFact(
            id=fact_id or str(uuid4()),
            kind=draft.kind,
            text=draft.text,
            condition_scope=draft.condition_scope,
            parent_action_fact_id=draft.parent_action_fact_id,
            evidence_refs=refs,
        )

    def _validate_fact_evidence(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        fact: FactDraft,
        allowed_segment_ids: set[str],
    ) -> None:
        for evidence in fact.evidence:
            if evidence.segment_id not in allowed_segment_ids:
                raise ValidationFailure("許可されていない根拠IDです。")
            self._resolve_evidence(connection, workspace_id, evidence)

    def _resolve_evidence(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        evidence: EvidenceDraft,
    ) -> EvidenceRef:
        row = connection.execute(
            "SELECT text, speaker FROM source_segments WHERE workspace_id = ? AND id = ?",
            (workspace_id, evidence.segment_id),
        ).fetchone()
        if row is None:
            raise ValidationFailure("存在しない根拠IDです。")
        if row["speaker"] == "assistant":
            raise ValidationFailure("AIの質問文は知識factの根拠にできません。")
        text = row["text"]
        occurrences = text.count(evidence.quote)
        if occurrences != 1:
            raise ValidationFailure(
                "引用が原文に存在しないか複数あるため、位置を自動決定できません。"
            )
        start = text.index(evidence.quote)
        return EvidenceRef(
            segment_id=evidence.segment_id,
            quote=evidence.quote,
            start_char=start,
            end_char=start + len(evidence.quote),
        )

    def _all_workspace_segment_ids(
        self, connection: sqlite3.Connection, workspace_id: str
    ) -> set[str]:
        return {
            row["id"]
            for row in connection.execute(
                "SELECT id FROM source_segments WHERE workspace_id = ?", (workspace_id,)
            ).fetchall()
        }

    def _source_kind_for_facts(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        facts: list[StoredFact],
    ) -> str:
        segment_ids = list(
            dict.fromkeys(ref.segment_id for fact in facts for ref in fact.evidence_refs)
        )
        if not segment_ids:
            raise ValidationFailure("知識項目に根拠がありません。")
        placeholders = ",".join("?" for _ in segment_ids)
        rows = connection.execute(
            f"""
            SELECT DISTINCT s.kind
            FROM source_segments ss JOIN sources s
              ON s.workspace_id = ss.workspace_id AND s.id = ss.source_id
            WHERE ss.workspace_id = ? AND ss.id IN ({placeholders})
            """,  # noqa: S608 - placeholders are generated from the validated list length
            [workspace_id, *segment_ids],
        ).fetchall()
        kinds = {str(row["kind"]) for row in rows}
        if kinds == {"document"}:
            return "document"
        if kinds == {"interview"}:
            return "interview"
        if kinds == {"document", "interview"}:
            return "mixed"
        raise ValidationFailure("知識項目の根拠由来を判定できません。")

    def _insert_answer_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        action_id: str,
        state: str,
        comparison: dict[str, Any],
        payload: dict[str, Any] | None,
        safe_error_code: str | None,
    ) -> None:
        stage = comparison.get("stage")
        if stage not in {"A", "B", "C"}:
            raise ValidationFailure("比較段階はA、B、Cのいずれかです。")
        question = comparison.get("question")
        conversation_id = comparison.get("conversation_id")
        model_id = comparison.get("model_id")
        prompt_version = comparison.get("prompt_version")
        schema_version = comparison.get("schema_version")
        retrieval_version = comparison.get("retrieval_version")
        model_settings = comparison.get("model_settings")
        if not all(
            isinstance(value, str) and value
            for value in (
                question,
                conversation_id,
                model_id,
                prompt_version,
                schema_version,
                retrieval_version,
            )
        ) or not isinstance(model_settings, dict):
            raise ValidationFailure("比較実行の設定記録が不足しています。")
        target_item_id = comparison.get("target_item_id")
        target_version = comparison.get("target_version")
        if target_item_id is not None and not isinstance(target_item_id, str):
            raise ValidationFailure("比較対象の知識IDが不正です。")
        if target_version is not None and not isinstance(target_version, int):
            raise ValidationFailure("比較対象の版が不正です。")
        question_text = cast(str, question)
        connection.execute(
            """
            INSERT OR IGNORE INTO answer_snapshots (
                workspace_id, id, stage, action_id, question, question_hash,
                conversation_id, empty_history, kb_revision, target_item_id,
                target_version, model_id, model_settings_json, prompt_version,
                schema_version, retrieval_version, state, payload_json,
                safe_error_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                str(uuid4()),
                stage,
                action_id,
                question_text,
                sha256_text(question_text),
                conversation_id,
                1 if comparison.get("empty_history") is True else 0,
                int(comparison.get("kb_revision", 0)),
                target_item_id,
                target_version,
                model_id,
                canonical_json(model_settings),
                prompt_version,
                schema_version,
                retrieval_version,
                state,
                canonical_json(payload) if payload is not None else None,
                safe_error_code,
                utc_now().isoformat(),
            ),
        )

    def _answer_snapshot_from_row(self, row: sqlite3.Row) -> AnswerSnapshotRecord:
        return AnswerSnapshotRecord(
            id=row["id"],
            stage=row["stage"],
            action_id=row["action_id"],
            question=row["question"],
            question_hash=row["question_hash"],
            conversation_id=row["conversation_id"],
            empty_history=bool(row["empty_history"]),
            kb_revision=row["kb_revision"],
            target_item_id=row["target_item_id"],
            target_version=row["target_version"],
            model_id=row["model_id"],
            model_settings=json.loads(row["model_settings_json"]),
            prompt_version=row["prompt_version"],
            schema_version=row["schema_version"],
            retrieval_version=row["retrieval_version"],
            state=row["state"],
            payload=json.loads(row["payload_json"]) if row["payload_json"] else None,
            safe_error_code=row["safe_error_code"],
            created_at=row["created_at"],
        )

    def _assert_workspace(self, connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        if row is None:
            raise AuthorizationError("作業領域を取得できません。")
        return cast(sqlite3.Row, row)

    def _knowledge_from_row(self, row: sqlite3.Row) -> KnowledgeRecord:
        try:
            facts = stored_facts_adapter.validate_json(row["facts_json"])
        except ValidationError as exc:
            raise ValidationFailure("保存済み知識の形式が破損しています。") from exc
        return KnowledgeRecord(
            workspace_id=row["workspace_id"],
            id=row["id"],
            display_name=f"知識項目{row['display_number']}",
            display_number=row["display_number"],
            title=row["title"] or f"知識項目{row['display_number']}",
            equipment=row["equipment"],
            case_label=row["case_label"],
            source_kind=row["source_kind"],
            tags=json.loads(row["tags_json"]),
            registration_origin=row["registration_origin"],
            origin_label=row["display_origin_label"],
            version=row["selected_version"],
            facts=facts,
            missing_fields=json.loads(row["missing_fields_json"]),
            cause_status=CauseStatus(row["cause_status"]),
        )

    def _proposal_from_row(self, row: sqlite3.Row) -> ProposalRecord:
        return ProposalRecord(
            workspace_id=row["workspace_id"],
            id=row["id"],
            target_item_id=row["target_item_id"],
            base_version=row["base_version"],
            operations=operations_adapter.validate_json(row["operations_json"]),
            reason=row["reason"],
            status=row["status"],
            content_hash=row["content_hash"],
            equipment=row["equipment"],
            case_label=row["case_label"],
            title=row["title"],
            missing_fields=json.loads(row["missing_fields_json"]),
            cause_status=CauseStatus(row["cause_status"]),
        )

    def _item_for_proposal(
        self, connection: sqlite3.Connection, workspace_id: str, proposal_id: str
    ) -> str:
        row = connection.execute(
            """
            SELECT item_id FROM knowledge_versions
            WHERE workspace_id = ? AND proposal_id = ?
            """,
            (workspace_id, proposal_id),
        ).fetchone()
        if row is None:
            raise ValidationFailure("承認済み更新の対象を確認できません。")
        return str(row["item_id"])
