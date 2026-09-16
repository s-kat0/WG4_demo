"""Authenticated approval facade."""

from __future__ import annotations

from wg4_demo.auth import AuthService
from wg4_demo.repository import ApprovalResult, Repository
from wg4_demo.schemas import Role


class ApprovalService:
    def __init__(self, repository: Repository, auth: AuthService) -> None:
        self.repository = repository
        self.auth = auth

    def approve(
        self,
        *,
        session_id: str,
        workspace_id: str,
        proposal_id: str,
        expected_content_hash: str,
    ) -> ApprovalResult:
        self.auth.require_session(session_id, role=Role.PARTICIPANT)
        self.repository.require_workspace(session_id, workspace_id)
        return self.repository.approve_proposal(
            workspace_id,
            proposal_id,
            actor_session_id=session_id,
            expected_content_hash=expected_content_hash,
        )

    def reject(self, *, session_id: str, workspace_id: str, proposal_id: str) -> None:
        self.auth.require_session(session_id, role=Role.PARTICIPANT)
        self.repository.require_workspace(session_id, workspace_id)
        self.repository.reject_proposal(workspace_id, proposal_id, actor_session_id=session_id)
