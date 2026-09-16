from __future__ import annotations

from pathlib import Path

import pytest

from wg4_demo.errors import ValidationFailure
from wg4_demo.repository import Repository
from wg4_demo.result_validation import ResultValidator, ToolTrace
from wg4_demo.retrieval import RetrievalService
from wg4_demo.schemas import AnswerCandidate, AnswerSelection, FactKind, SessionRecord


def test_nonexistent_or_unread_evidence_rejects_entire_answer(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace = repository.create_workspace(
        participant.id,
        seed_mode="approved_v1",
        seed_path=project_root / "data" / "approved_seed.json",
    )
    retrieval = RetrievalService(repository, project_root / "data" / "vocabulary.json")
    search = retrieval.search_knowledge(
        workspace.id,
        "冷却器1の出口温度の表示が高く、温度計交換直後で流量と入口温度は通常",
    )
    hit = search.hits[0]
    item = repository.get_knowledge(workspace.id, hit.knowledge_id, hit.version)
    action = next(fact for fact in item.facts if fact.kind is FactKind.CHECK_ACTION)
    conditions = [fact.id for fact in item.facts if fact.kind is FactKind.CONDITION]
    answer = AnswerSelection(
        status="candidates",
        candidates=[
            AnswerCandidate(
                knowledge_id=item.id,
                version=item.version,
                action_fact_id=action.id,
                condition_fact_ids=conditions,
                evidence_segment_ids=["similar-but-nonexistent-id"],
            )
        ],
        clarification_requests=[],
        conflicting_fact_ids=[],
    )
    trace = ToolTrace(
        search_result=search,
        context_versions={(item.id, item.version)},
        evidence_ids=set(),
        calls=["search_knowledge", "get_context", "read_evidence"],
    )

    with pytest.raises(ValidationFailure) as exc_info:
        ResultValidator(repository).validate_answer(workspace.id, answer, trace)
    assert exc_info.value.code == "validation_unread_evidence"
