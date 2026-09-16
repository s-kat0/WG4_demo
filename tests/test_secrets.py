from __future__ import annotations

from pathlib import Path

from wg4_demo.auth import AuthService
from wg4_demo.export import ExportService
from wg4_demo.repository import Repository
from wg4_demo.safe_display import safe_error
from wg4_demo.schemas import SessionRecord
from wg4_demo.settings import Settings


def test_secrets_are_masked_and_absent_from_export_and_safe_errors(
    settings: Settings,
    auth: AuthService,
    participant: SessionRecord,
    repository: Repository,
    project_root: Path,
) -> None:
    canary = settings.openai_api_key.get_secret_value() if settings.openai_api_key else "missing"
    assert canary not in repr(settings)
    workspace = repository.create_workspace(
        participant.id,
        seed_mode="approved_v1",
        seed_path=project_root / "data" / "approved_seed.json",
    )
    exported = ExportService(repository, auth).export_json(
        session_id=participant.id, workspace_id=workspace.id
    )
    assert canary.encode() not in exported
    code, message = safe_error(RuntimeError(f"internal {canary}"))
    assert code == "internal_error"
    assert canary not in message
