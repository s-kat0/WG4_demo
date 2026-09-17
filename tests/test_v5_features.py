from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from wg4_demo.conversation import ConversationService
from wg4_demo.errors import SearchFailure, ValidationFailure
from wg4_demo.repository import Repository
from wg4_demo.retrieval import RetrievalService
from wg4_demo.schemas import (
    CauseStatus,
    ConsultationIntent,
    EvidenceDraft,
    FactDraft,
    FactKind,
    OperationType,
    ProposalOperation,
    SessionRecord,
)
from wg4_demo.seed_v5 import load_v5_seed


def create_v5_workspace(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> str:
    return repository.create_workspace(
        participant.id,
        seed_mode="practical_v5",
        seed_path=project_root / "data" / "knowledge_seed_v5.json",
    ).id


def test_v5_seed_imports_twelve_independent_items_with_valid_evidence(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace_id = create_v5_workspace(repository, participant, project_root)
    items = repository.list_knowledge(workspace_id)

    assert len(items) == 12
    assert sum(item.source_kind == "document" for item in items) == 6
    assert sum(item.source_kind == "interview" for item in items) == 6
    assert {item.origin_label for item in items} == {"初期収録（架空教材）"}
    assert len({item.id for item in items}) == 12
    assert sum(len(item.facts) for item in items) == 41
    assert repository.knowledge_stats(workspace_id) == {
        "knowledge_count": 12,
        "source_counts": {"document": 6, "interview": 6},
        "equipment_count": 3,
        "equipment_counts": {"冷却器1": 8, "ポンプ1": 2, "貯槽1": 2},
    }
    for item in items:
        for fact in item.facts:
            for ref in fact.evidence_refs:
                segment = repository.read_segments(workspace_id, [ref.segment_id])[0]
                assert ref.quote in segment["text"]
                assert segment["speaker"] != "assistant"
    interview_item = next(item for item in items if item.source_kind == "interview")
    interview_evidence = [
        ref.segment_id for fact in interview_item.facts for ref in fact.evidence_refs
    ]
    transcript = repository.read_source_transcripts(workspace_id, interview_evidence)
    assert {segment["speaker"] for segment in transcript} == {"assistant", "operator"}


def test_v5_seed_is_idempotent_per_workspace_and_does_not_modify_legacy(
    repository: Repository,
    participant: SessionRecord,
    project_root: Path,
    auth,
) -> None:
    legacy = repository.create_workspace(
        participant.id,
        seed_mode="approved_v1",
        seed_path=project_root / "data" / "approved_seed.json",
    )
    legacy_ids = [item.id for item in repository.list_knowledge(legacy.id)]
    same = repository.create_workspace(
        participant.id,
        seed_mode="practical_v5",
        seed_path=project_root / "data" / "knowledge_seed_v5.json",
    )
    assert same.id == legacy.id
    assert [item.id for item in repository.list_knowledge(legacy.id)] == legacy_ids

    new_participant = auth.login(
        "participant-secret", role=participant.role, client_token="v5-new-workspace"
    )
    new_workspace = repository.create_workspace(
        new_participant.id,
        seed_mode="practical_v5",
        seed_path=project_root / "data" / "knowledge_seed_v5.json",
    )
    assert len(repository.list_knowledge(new_workspace.id)) == 12
    assert [item.id for item in repository.list_knowledge(legacy.id)] == legacy_ids


def test_v5_seed_rejects_invalid_quote_atomically(
    repository: Repository,
    participant: SessionRecord,
    project_root: Path,
    tmp_path: Path,
) -> None:
    payload = json.loads((project_root / "data" / "knowledge_seed_v5.json").read_text("utf-8"))
    payload["knowledge_items"][0]["facts"][0]["evidence"][0]["quote"] = "存在しない引用"
    invalid = tmp_path / "invalid-seed.json"
    invalid.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValidationFailure, match="seed_v5_invalid"):
        repository.create_workspace(
            participant.id,
            seed_mode="practical_v5",
            seed_path=invalid,
        )
    assert repository.workspace_for_session(participant.id) is None


@pytest.mark.parametrize(
    ("query", "expected_numbers"),
    [
        ("冷却器1 流量低下", {2, 8}),
        ("起動直後の記録を別に探す理由", {4, 10}),
        ("清掃履歴 原因", {9}),
        ("ポンプ1 振動", {5, 11}),
        ("貯槽1 液位 設計", {6, 12}),
    ],
)
def test_v5_browse_queries_find_expected_items(
    repository: Repository,
    participant: SessionRecord,
    project_root: Path,
    query: str,
    expected_numbers: set[int],
) -> None:
    workspace_id = create_v5_workspace(repository, participant, project_root)
    retrieval = RetrievalService(repository, project_root / "data" / "vocabulary.json")

    result = retrieval.search_knowledge(workspace_id, query)

    found = {int(hit.display_name.removeprefix("知識項目")) for hit in result.hits}
    assert found & expected_numbers


def test_search_filters_are_workspace_scoped_and_non_mutating(
    repository: Repository,
    participant: SessionRecord,
    project_root: Path,
) -> None:
    workspace_id = create_v5_workspace(repository, participant, project_root)
    retrieval = RetrievalService(repository, project_root / "data" / "vocabulary.json")
    before = repository.require_workspace(participant.id, workspace_id).kb_revision

    result = retrieval.search_knowledge(
        workspace_id, "", equipment_name="冷却器1", source_kind="interview", limit=20
    )

    assert len(result.hits) == 4
    assert all(hit.equipment == "冷却器1" for hit in result.hits)
    assert all(hit.source_kind == "interview" for hit in result.hits)
    assert repository.require_workspace(participant.id, workspace_id).kb_revision == before


def test_search_zero_and_search_failure_are_distinct(
    repository: Repository,
    participant: SessionRecord,
    project_root: Path,
    monkeypatch,
) -> None:
    workspace_id = create_v5_workspace(repository, participant, project_root)
    retrieval = RetrievalService(repository, project_root / "data" / "vocabulary.json")
    assert retrieval.search_knowledge(workspace_id, "qqqxyzzz").hits == []

    def fail_read(_workspace_id: str, _ids: object) -> list[dict[str, object]]:
        raise OSError("simulated read failure")

    monkeypatch.setattr(repository, "read_segments", fail_read)
    with pytest.raises(SearchFailure):
        retrieval.search_knowledge(workspace_id, "冷却器1")


def test_conversation_keeps_hypothesis_separate_and_clears_on_equipment_switch(
    repository: Repository,
    participant: SessionRecord,
    project_root: Path,
) -> None:
    workspace_id = create_v5_workspace(repository, participant, project_root)
    conversation_id = repository.create_conversation(workspace_id)
    service = ConversationService(repository)

    first = service.prepare_turn(
        workspace_id,
        conversation_id,
        "冷却器1の出口温度が高い。流量は通常です。",
    )
    hypothetical = service.prepare_turn(
        workspace_id,
        conversation_id,
        "流量が低かった場合も同じですか。",
    )
    corrected = service.prepare_turn(
        workspace_id,
        conversation_id,
        "訂正します。実際には流量が低かったです。",
    )
    switched = service.prepare_turn(
        workspace_id,
        conversation_id,
        "ポンプ1の振動について確認したい。",
    )

    assert first.equipment == "冷却器1"
    assert hypothetical.last_intent is ConsultationIntent.CONDITION_COMPARISON
    assert hypothetical.actual_context == first.actual_context
    assert hypothetical.hypothetical_context == ["流量が低かった場合も同じですか。"]
    assert corrected.hypothetical_context == []
    assert "実際には流量が低かった" in corrected.actual_context[-1]
    assert switched.equipment == "ポンプ1"
    assert switched.actual_context == ["ポンプ1の振動について確認したい。"]
    assert switched.hypothetical_context == []


def test_explicit_selection_marker_is_scoped_to_the_prepared_turn(
    repository: Repository,
    participant: SessionRecord,
    project_root: Path,
) -> None:
    workspace_id = create_v5_workspace(repository, participant, project_root)
    conversation_id = repository.create_conversation(workspace_id)
    selected = repository.list_knowledge(workspace_id)[0]
    service = ConversationService(repository)
    state = service.prepare_turn(
        workspace_id,
        conversation_id,
        "この知識について確認候補を教えてください。",
        selected_knowledge_id=selected.id,
    )

    explicit = service.payload_for_turn(state, explicit_selected_knowledge_id=selected.id)
    ordinary = service.payload_for_turn(state)

    assert explicit["explicit_selected_knowledge_id"] == selected.id
    assert "explicit_selected_knowledge_id" not in ordinary


def test_ambiguous_pronoun_is_marked_before_api_call(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace_id = create_v5_workspace(repository, participant, project_root)
    conversation_id = repository.create_conversation(workspace_id)

    state = ConversationService(repository).prepare_turn(
        workspace_id, conversation_id, "それはなぜですか。"
    )

    assert state.last_intent is ConsultationIntent.REASON_EXPLANATION
    assert state.reference_is_ambiguous is True


def test_proposed_seed_file_itself_validates(project_root: Path) -> None:
    manifest = load_v5_seed(project_root / "data" / "knowledge_seed_v5.json")
    assert manifest.import_contract.llm_calls_required == 0


def test_document_then_interview_updates_one_dynamic_lecture_item(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace_id = create_v5_workspace(repository, participant, project_root)
    _, document_mapping = repository.register_source(
        workspace_id,
        title="保全記録1",
        kind="document",
        equipment="冷却器1",
        case_label="事例1",
        segments=[
            (
                "lecture-document",
                "document",
                "温度計交換後に出口温度表示が高くなり、別計器と照合した。原因は未特定。",
            )
        ],
    )
    document_segment = document_mapping["lecture-document"]
    document_operations = [
        ProposalOperation(
            operation=OperationType.ADD_FACT,
            new_fact=FactDraft(
                kind=FactKind.OBSERVATION,
                text="出口温度表示が高い",
                evidence=[
                    EvidenceDraft(segment_id=document_segment, quote="出口温度表示が高くなり")
                ],
            ),
        ),
        ProposalOperation(
            operation=OperationType.ADD_FACT,
            new_fact=FactDraft(
                kind=FactKind.CHECK_ACTION,
                text="別計器と照合する",
                evidence=[EvidenceDraft(segment_id=document_segment, quote="別計器と照合した")],
            ),
        ),
        ProposalOperation(
            operation=OperationType.ADD_FACT,
            new_fact=FactDraft(
                kind=FactKind.CAUSE_STATUS,
                text="原因は未特定",
                evidence=[EvidenceDraft(segment_id=document_segment, quote="原因は未特定")],
            ),
        ),
    ]
    proposal = repository.stage_proposal(
        workspace_id,
        action_id="dynamic-document",
        target_item_id=None,
        base_version=0,
        operations=document_operations,
        reason="文書版",
        equipment="冷却器1",
        case_label="事例1",
        title="講演対象事例",
        missing_fields=["判断理由", "適用範囲"],
        cause_status=CauseStatus.UNRESOLVED,
        allowed_segment_ids={document_segment},
    )
    repository.publish_proposal(workspace_id, proposal.id)
    approved = repository.approve_proposal(
        workspace_id,
        proposal.id,
        actor_session_id=participant.id,
        expected_content_hash=proposal.content_hash,
    )
    workspace = repository.require_workspace(participant.id, workspace_id)
    assert len(repository.list_knowledge(workspace_id)) == 13
    assert workspace.lecture_case_item_id == approved.item_id
    assert repository.get_knowledge(workspace_id, approved.item_id).display_number == 13

    _, interview_mapping = repository.register_source(
        workspace_id,
        title="聞き取り記録1",
        kind="interview",
        equipment="冷却器1",
        case_label="事例1",
        segments=[
            (
                "lecture-reason",
                "operator",
                "表示の変化か実際の温度変化かを切り分けるため、別計器で確かめました。",
            )
        ],
    )
    interview_segment = interview_mapping["lecture-reason"]
    supplement = repository.stage_proposal(
        workspace_id,
        action_id="dynamic-interview",
        target_item_id=approved.item_id,
        base_version=1,
        operations=[
            ProposalOperation(
                operation=OperationType.ADD_FACT,
                new_fact=FactDraft(
                    kind=FactKind.DECISION_REASON,
                    text="表示変化と実温度変化を切り分けるため",
                    evidence=[
                        EvidenceDraft(
                            segment_id=interview_segment,
                            quote="表示の変化か実際の温度変化かを切り分けるため",
                        )
                    ],
                ),
            )
        ],
        reason="聞き取り補足",
        equipment="冷却器1",
        case_label="事例1",
        missing_fields=["適用範囲"],
        cause_status=CauseStatus.UNRESOLVED,
        allowed_segment_ids={interview_segment},
    )
    repository.publish_proposal(workspace_id, supplement.id)
    assert interview_segment not in repository.allowed_evidence_ids(workspace_id)
    assert all(
        fact.kind is not FactKind.DECISION_REASON
        for fact in repository.get_knowledge(workspace_id, approved.item_id).facts
    )
    updated = repository.approve_proposal(
        workspace_id,
        supplement.id,
        actor_session_id=participant.id,
        expected_content_hash=supplement.content_hash,
    )
    item = repository.get_knowledge(workspace_id, approved.item_id)
    assert updated.after_version == 2
    assert item.source_kind == "mixed"
    assert repository.get_knowledge(workspace_id, approved.item_id, 1).source_kind == "document"
    assert len(repository.list_knowledge(workspace_id)) == 13
    assert any(fact.kind is FactKind.DECISION_REASON for fact in item.facts)
    assert interview_segment in repository.allowed_evidence_ids(workspace_id)


def test_answer_snapshots_keep_real_success_and_failure_records(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace_id = create_v5_workspace(repository, participant, project_root)
    conversation_id = repository.create_conversation(workspace_id)
    workspace = repository.require_workspace(participant.id, workspace_id)
    action_id = "comparison-a-action"
    repository.begin_action_guard(
        workspace_id,
        action_id=action_id,
        claim_token="claim-a",
        kb_revision=workspace.kb_revision,
    )
    question = "冷却器1では何を確認するか"
    comparison = {
        "stage": "A",
        "question": question,
        "conversation_id": conversation_id,
        "empty_history": True,
        "kb_revision": workspace.kb_revision,
        "target_item_id": None,
        "target_version": 1,
        "model_id": "test-model",
        "model_settings": {"store": False},
        "prompt_version": "wg4-prompts-v15",
        "schema_version": "wg4-schema-v2",
        "retrieval_version": "wg4-lexical-v2",
    }
    payload = {
        "selection": {
            "intent": "candidate_search",
            "status": "insufficient_evidence",
            "candidates": [],
            "clarification_requests": [],
            "conflicting_fact_ids": [],
        },
        "tools": ["search_knowledge"],
        "conversation_id": conversation_id,
        "comparison": comparison,
    }
    repository.save_action_outcome(
        workspace_id,
        action_id=action_id,
        session_id=participant.id,
        claim_token="claim-a",
        input_sha256="input-a",
        kb_revision=workspace.kb_revision,
        outcome_type="answer",
        payload=payload,
    )
    repository.record_answer_snapshot_failure(
        workspace_id,
        action_id="comparison-b-action",
        comparison={**comparison, "stage": "B"},
        safe_error_code="api_state_unknown",
    )

    snapshots = repository.list_answer_snapshots(workspace_id)
    assert [(snapshot.stage, snapshot.state) for snapshot in snapshots] == [
        ("A", "succeeded"),
        ("B", "failed"),
    ]
    assert snapshots[0].payload == payload
    assert snapshots[1].payload is None
    assert snapshots[1].safe_error_code == "api_state_unknown"


def test_schema_v1_workspace_is_migrated_additively(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE domain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO domain_meta (key, value) VALUES ('schema_version', '1');
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                kb_revision INTEGER NOT NULL DEFAULT 0,
                seed_mode TEXT NOT NULL
            );
            CREATE TABLE knowledge_items (
                workspace_id TEXT NOT NULL,
                id TEXT NOT NULL,
                display_number INTEGER NOT NULL,
                equipment TEXT NOT NULL,
                case_label TEXT,
                active_version INTEGER NOT NULL,
                PRIMARY KEY (workspace_id, id)
            );
            CREATE TABLE proposals (
                workspace_id TEXT NOT NULL,
                id TEXT NOT NULL,
                target_item_id TEXT,
                base_version INTEGER NOT NULL,
                operations_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                equipment TEXT NOT NULL,
                case_label TEXT,
                missing_fields_json TEXT NOT NULL,
                cause_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                action_id TEXT NOT NULL,
                PRIMARY KEY (workspace_id, id)
            );
            CREATE TABLE conversations (
                workspace_id TEXT NOT NULL,
                id TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (workspace_id, id)
            );
            INSERT INTO workspaces VALUES (
                'legacy-workspace', 'legacy-session', '2026-09-15T00:00:00+00:00',
                '2026-09-15T00:00:00+00:00', 7, 'approved_v1'
            );
            INSERT INTO conversations VALUES (
                'legacy-workspace', 'legacy-conversation', 'qa',
                '2026-09-15T00:00:00+00:00'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    repository = Repository(database)

    workspace = repository.require_workspace("legacy-session", "legacy-workspace")
    state = repository.get_conversation_state("legacy-workspace", "legacy-conversation")
    assert workspace.kb_revision == 7
    assert workspace.seed_mode == "approved_v1"
    assert workspace.lecture_case_item_id is None
    assert state.actual_context == []
