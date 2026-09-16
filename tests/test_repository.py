from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wg4_demo.errors import AuthorizationError, ValidationFailure
from wg4_demo.repository import Repository
from wg4_demo.schemas import (
    CauseStatus,
    ConditionScope,
    EvidenceDraft,
    FactDraft,
    FactKind,
    OperationType,
    ProposalOperation,
    SessionRecord,
)


def create_seeded_workspace(
    repository: Repository,
    participant: SessionRecord,
    project_root: Path,
    *,
    mode: str = "approved_v1",
) -> str:
    return repository.create_workspace(
        participant.id,
        seed_mode=mode,
        seed_path=project_root / "data" / "approved_seed.json",
    ).id


def test_seed_is_transformed_to_approved_versions(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace_id = create_seeded_workspace(repository, participant, project_root)
    items = repository.list_knowledge(workspace_id)

    assert [item.display_name for item in items] == ["知識項目1", "知識項目2", "知識項目3"]
    assert all(item.version == 1 for item in items)
    assert all(item.cause_status is CauseStatus.UNRESOLVED for item in items)
    assert [item.source_kind for item in items] == ["document", "document", "interview"]
    assert repository.allowed_evidence_ids(workspace_id)

    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(
            "UPDATE knowledge_items SET source_kind = 'document', title = '' WHERE workspace_id = ? AND display_number = 3",
            (workspace_id,),
        )
        connection.commit()
    finally:
        connection.close()
    reloaded = Repository(repository.path)
    corrected = reloaded.list_knowledge(workspace_id)[2]
    assert corrected.source_kind == "interview"
    assert corrected.title == "知識項目3"


def test_workspace_ids_are_enforced(
    repository: Repository, participant: SessionRecord, project_root: Path, auth
) -> None:
    workspace_a = create_seeded_workspace(repository, participant, project_root)
    participant_b = auth.login(
        "participant-secret", role=participant.role, client_token="browser-b"
    )
    workspace_b = create_seeded_workspace(repository, participant_b, project_root)
    item_a = repository.list_knowledge(workspace_a)[0]

    with pytest.raises(AuthorizationError):
        repository.get_knowledge(workspace_b, item_a.id)
    with pytest.raises(AuthorizationError):
        repository.require_workspace(participant_b.id, workspace_a)


def test_unapproved_source_stays_out_until_atomic_approval(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace_id = create_seeded_workspace(repository, participant, project_root)
    item3 = repository.list_knowledge(workspace_id)[2]
    action = next(fact for fact in item3.facts if fact.kind is FactKind.CHECK_ACTION)
    _, mapping = repository.register_source(
        workspace_id,
        title="聞き取り記録2",
        kind="interview",
        equipment="冷却器1",
        case_label="事例1",
        segments=[
            (
                "interview2-p1",
                "operator",
                "照合用の計器も、校正が有効か確認する必要があります。",
            )
        ],
    )
    segment_id = mapping["interview2-p1"]
    operation = ProposalOperation(
        operation=OperationType.ADD_FACT,
        new_fact=FactDraft(
            kind=FactKind.CONDITION,
            text="照合用計器の校正が有効か確認する",
            condition_scope=ConditionScope.ACTION_PREREQUISITE,
            parent_action_fact_id=action.id,
            evidence=[
                EvidenceDraft(
                    segment_id=segment_id,
                    quote="照合用の計器も、校正が有効か確認する必要があります。",
                )
            ],
        ),
    )
    proposal = repository.stage_proposal(
        workspace_id,
        action_id="action-update-1",
        target_item_id=item3.id,
        base_version=1,
        operations=[operation],
        reason="照合に必要な確認事項を補う",
        equipment="冷却器1",
        case_label="事例1",
        missing_fields=item3.missing_fields,
        cause_status=item3.cause_status,
        allowed_segment_ids={segment_id},
    )
    same_proposal = repository.stage_proposal(
        workspace_id,
        action_id="action-update-1",
        target_item_id=item3.id,
        base_version=1,
        operations=[operation],
        reason="照合に必要な確認事項を補う",
        equipment="冷却器1",
        case_label="事例1",
        missing_fields=item3.missing_fields,
        cause_status=item3.cause_status,
        allowed_segment_ids={segment_id},
    )
    stale_proposal = repository.stage_proposal(
        workspace_id,
        action_id="action-update-2",
        target_item_id=item3.id,
        base_version=1,
        operations=[operation],
        reason="同じ旧版を対象にする競合案",
        equipment="冷却器1",
        case_label="事例1",
        missing_fields=item3.missing_fields,
        cause_status=item3.cause_status,
        allowed_segment_ids={segment_id},
    )

    assert proposal.status == "staged"
    assert same_proposal.id == proposal.id
    assert segment_id not in repository.allowed_evidence_ids(workspace_id)
    proposal = repository.publish_proposal(workspace_id, proposal.id)
    stale_proposal = repository.publish_proposal(workspace_id, stale_proposal.id)
    assert proposal.status == "pending"
    assert segment_id not in repository.allowed_evidence_ids(workspace_id)

    result = repository.approve_proposal(
        workspace_id,
        proposal.id,
        actor_session_id=participant.id,
        expected_content_hash=proposal.content_hash,
    )
    assert result.after_version == 2
    assert segment_id in repository.allowed_evidence_ids(workspace_id)
    updated = repository.get_knowledge(workspace_id, item3.id)
    prerequisite = next(
        fact for fact in updated.facts if fact.condition_scope is ConditionScope.ACTION_PREREQUISITE
    )
    assert "確認" in prerequisite.text
    assert "有効だった" not in prerequisite.text

    duplicate = repository.approve_proposal(
        workspace_id,
        proposal.id,
        actor_session_id=participant.id,
        expected_content_hash=proposal.content_hash,
    )
    assert duplicate.already_applied is True
    assert repository.get_knowledge(workspace_id, item3.id).version == 2

    with pytest.raises(ValidationFailure, match="proposal_stale"):
        repository.approve_proposal(
            workspace_id,
            stale_proposal.id,
            actor_session_id=participant.id,
            expected_content_hash=stale_proposal.content_hash,
        )
    assert repository.get_proposal(workspace_id, stale_proposal.id).status == "stale"

    old_conversation = repository.create_conversation(workspace_id)
    repository.append_message(workspace_id, old_conversation, role="user", text="old question")
    new_conversation = repository.create_conversation(workspace_id)
    assert repository.list_messages(workspace_id, new_conversation) == []
    assert repository.get_knowledge(workspace_id, item3.id).version == 2


def test_from_scratch_workspace_and_reset_are_really_empty(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace = repository.create_workspace(participant.id, seed_mode="from_scratch")
    assert repository.list_knowledge(workspace.id) == []

    with pytest.raises(ValidationFailure, match="seed_unexpected"):
        repository.reset_workspace(
            participant.id,
            workspace.id,
            seed_mode="from_scratch",
            seed_path=project_root / "data" / "approved_seed.json",
        )

    reset, _ = repository.reset_workspace(
        participant.id,
        workspace.id,
        seed_mode="from_scratch",
        seed_path=None,
    )
    assert reset.kb_revision == 1
    assert repository.list_knowledge(workspace.id) == []


def test_quote_mismatch_is_not_repaired(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace_id = create_seeded_workspace(repository, participant, project_root)
    item = repository.list_knowledge(workspace_id)[0]
    _, mapping = repository.register_source(
        workspace_id,
        title="入力",
        kind="interview",
        equipment="冷却器1",
        case_label=None,
        segments=[("new-p1", "operator", "原文です。")],
    )
    with pytest.raises(ValidationFailure):
        repository.stage_proposal(
            workspace_id,
            action_id="invalid-evidence",
            target_item_id=item.id,
            base_version=item.version,
            operations=[
                ProposalOperation(
                    operation=OperationType.ADD_FACT,
                    new_fact=FactDraft(
                        kind=FactKind.OBSERVATION,
                        text="原文にない観察",
                        evidence=[
                            EvidenceDraft(segment_id=mapping["new-p1"], quote="存在しない引用")
                        ],
                    ),
                )
            ],
            reason="テスト",
            equipment=item.equipment,
            case_label=item.case_label,
            missing_fields=item.missing_fields,
            cause_status=item.cause_status,
            allowed_segment_ids={mapping["new-p1"]},
        )
