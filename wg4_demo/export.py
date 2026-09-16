"""Export only one authenticated workspace's approved domain data."""

from __future__ import annotations

import json

from wg4_demo.auth import AuthService
from wg4_demo.repository import Repository
from wg4_demo.schemas import Role


class ExportService:
    def __init__(self, repository: Repository, auth: AuthService) -> None:
        self.repository = repository
        self.auth = auth

    def export_json(self, *, session_id: str, workspace_id: str) -> bytes:
        self.auth.require_session(session_id, role=Role.PARTICIPANT)
        workspace = self.repository.require_workspace(session_id, workspace_id)
        items = self.repository.list_knowledge(workspace_id)
        allowed = sorted(self.repository.allowed_evidence_ids(workspace_id))
        segments = self.repository.read_source_transcripts(workspace_id, allowed) if allowed else []
        payload = {
            "export_version": "wg4-export-v1",
            "kb_revision": workspace.kb_revision,
            "knowledge": [
                {
                    "display_name": item.display_name,
                    "title": item.title,
                    "version": item.version,
                    "equipment": item.equipment,
                    "case_label": item.case_label,
                    "source_kind": item.source_kind,
                    "origin_label": item.origin_label,
                    "registration_origin": item.registration_origin,
                    "facts": [fact.model_dump(mode="json") for fact in item.facts],
                    "missing_fields": item.missing_fields,
                    "cause_status": item.cause_status.value,
                }
                for item in items
            ],
            "sources": [
                {
                    "segment_id": segment["id"],
                    "title": segment["title"],
                    "text": segment["text"],
                    "speaker": segment["speaker"],
                }
                for segment in segments
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
