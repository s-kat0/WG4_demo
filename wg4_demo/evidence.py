"""Evidence access constrained by active approved versions."""

from __future__ import annotations

from wg4_demo.errors import AuthorizationError
from wg4_demo.repository import Repository


class EvidenceService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def read_for_qa(self, workspace_id: str, segment_ids: list[str]) -> list[dict[str, object]]:
        if len(segment_ids) > 6:
            raise AuthorizationError("一度に取得できる根拠は6件までです。")
        allowed = self.repository.allowed_evidence_ids(workspace_id)
        if not set(segment_ids).issubset(allowed):
            raise AuthorizationError("未承認または別作業領域の根拠は取得できません。")
        return self.repository.read_segments(workspace_id, segment_ids)

    def read_for_update(
        self,
        workspace_id: str,
        segment_ids: list[str],
        *,
        submitted_segment_ids: set[str],
    ) -> list[dict[str, object]]:
        if len(segment_ids) > 6:
            raise AuthorizationError("一度に取得できる根拠は6件までです。")
        allowed = self.repository.allowed_evidence_ids(workspace_id) | submitted_segment_ids
        if not set(segment_ids).issubset(allowed):
            raise AuthorizationError("この更新操作で許可されていない根拠です。")
        return self.repository.read_segments(workspace_id, segment_ids)
