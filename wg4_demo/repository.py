"""Workspace-scoped SQLite repository and atomic proposal approval."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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
    EvidenceDraft,
    EvidenceRef,
    FactDraft,
    FactKind,
    OperationType,
    ProposalOperation,
    StoredFact,
)

DOMAIN_SCHEMA_VERSION = 1
stored_facts_adapter = TypeAdapter(list[StoredFact])
operations_adapter = TypeAdapter(list[ProposalOperation])


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


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    workspace_id: str
    id: str
    display_name: str
    equipment: str
    case_label: str | None
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
                        seed_mode TEXT NOT NULL
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
                        equipment TEXT NOT NULL,
                        case_label TEXT,
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
                elif int(current["value"]) != DOMAIN_SCHEMA_VERSION:
                    raise RuntimeError("unsupported domain schema")
        finally:
            connection.close()

    def create_workspace(
        self, session_id: str, *, seed_mode: str, seed_path: Path | None = None
    ) -> WorkspaceRecord:
        if seed_mode not in {"from_scratch", "approved_v1"}:
            raise ValueError("unknown seed mode")
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
        return WorkspaceRecord(workspace_id, session_id, revision, seed_mode)

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
        return WorkspaceRecord(row["id"], row["session_id"], row["kb_revision"], row["seed_mode"])

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
        return WorkspaceRecord(row["id"], row["session_id"], row["kb_revision"], row["seed_mode"])

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
        finally:
            connection.close()
        return conversation_id

    def reset_workspace(
        self,
        session_id: str,
        workspace_id: str,
        *,
        seed_mode: str,
        seed_path: Path,
    ) -> tuple[WorkspaceRecord, str]:
        if seed_mode not in {"from_scratch", "approved_v1"}:
            raise ValueError("unknown seed mode")
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
                    "DELETE FROM workspace_guards WHERE workspace_id = ?", (workspace_id,)
                )
                self._seed_workspace(connection, workspace_id, seed_path)
                if seed_mode == "approved_v1":
                    self._seed_item3_v1(connection, workspace_id)
                revision = old_revision + 1
                now = utc_now().isoformat()
                connection.execute(
                    """
                    UPDATE workspaces SET kb_revision = ?, seed_mode = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (revision, seed_mode, now, workspace_id),
                )
                connection.execute(
                    "INSERT INTO conversations (workspace_id, id, mode, created_at) VALUES (?, ?, 'qa', ?)",
                    (workspace_id, conversation_id, now),
                )
        finally:
            connection.close()
        return WorkspaceRecord(workspace_id, session_id, revision, seed_mode), conversation_id

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
                SELECT ki.*, kv.facts_json, kv.missing_fields_json, kv.cause_status
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
                SELECT ki.*, kv.facts_json, kv.missing_fields_json, kv.cause_status
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
        return self._knowledge_from_row(row)

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
    ) -> ProposalRecord:
        if not operations:
            raise ValidationFailure("変更内容が空です。")
        connection = connect_sqlite(self.path)
        proposal_id = str(uuid4())
        try:
            with transaction(connection, immediate=True):
                self._assert_workspace(connection, workspace_id)
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
                payload = {
                    "target_item_id": target_item_id,
                    "base_version": base_version,
                    "operations": [op.model_dump(mode="json") for op in operations],
                    "reason": reason,
                    "equipment": equipment,
                    "case_label": case_label,
                    "missing_fields": missing_fields,
                    "cause_status": cause_status.value,
                }
                content_hash = sha256_text(canonical_json(payload))
                connection.execute(
                    """
                    INSERT INTO proposals (
                        workspace_id, id, target_item_id, base_version, operations_json,
                        reason, status, content_hash, equipment, case_label,
                        missing_fields_json, cause_status, created_at, action_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?, ?, ?, ?)
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
                            workspace_id, id, display_number, equipment, case_label, active_version
                        ) VALUES (?, ?, ?, ?, ?, 0)
                        """,
                        (
                            workspace_id,
                            target_id,
                            display_number,
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
                        connection.execute(
                            "UPDATE proposals SET status = 'stale' WHERE workspace_id = ? AND id = ?",
                            (workspace_id, proposal_id),
                        )
                        raise ValidationFailure("現行版が進んだため、この更新案はstaleです。")
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
                    UPDATE knowledge_items SET active_version = ?
                    WHERE workspace_id = ? AND id = ?
                    """,
                    (after_version, workspace_id, target_id),
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
                connection.execute(
                    """
                    UPDATE workspaces SET kb_revision = ?, updated_at = ? WHERE id = ?
                    """,
                    (revision, utc_now().isoformat(), workspace_id),
                )
                return ApprovalResult(
                    proposal_id, target_id, before_version, after_version, revision, False
                )
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
        payload = json.loads(path.read_text(encoding="utf-8"))
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
                    workspace_id, id, display_number, equipment, case_label, active_version
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    workspace_id,
                    item_id,
                    display_number,
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
                workspace_id, id, display_number, equipment, case_label, active_version
            ) VALUES (?, ?, ?, '冷却器1', '事例1', 1)
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
            ) VALUES (?, ?, 1, ?, ?, ?, ?, 'fixture_author', NULL, ?)
            """,
            (
                workspace_id,
                item_id,
                facts_json,
                canonical_json(missing_fields),
                cause_status.value,
                utc_now().isoformat(),
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
            "SELECT text FROM source_segments WHERE workspace_id = ? AND id = ?",
            (workspace_id, evidence.segment_id),
        ).fetchone()
        if row is None:
            raise ValidationFailure("存在しない根拠IDです。")
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
            equipment=row["equipment"],
            case_label=row["case_label"],
            version=row["active_version"],
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
