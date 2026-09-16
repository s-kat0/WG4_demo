"""Validate agent selections against actually executed tool results."""

from __future__ import annotations

from dataclasses import dataclass, field

from wg4_demo.errors import ValidationFailure
from wg4_demo.repository import Repository
from wg4_demo.schemas import AnswerSelection, ConditionScope, FactKind, SearchSuccess


@dataclass(slots=True)
class ToolTrace:
    search_result: SearchSuccess | None = None
    context_versions: set[tuple[str, int]] = field(default_factory=set)
    evidence_ids: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)


class ResultValidator:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def validate_answer(
        self, workspace_id: str, answer: AnswerSelection, trace: ToolTrace
    ) -> AnswerSelection:
        if trace.search_result is None or "search_knowledge" not in trace.calls:
            raise ValidationFailure(
                "検索が正常完了した証跡がありません。",
                code="validation_search_trace_missing",
            )
        if answer.status == "insufficient_evidence":
            if trace.search_result.hits:
                raise ValidationFailure(
                    "取得候補があるため、根拠なしとして扱えません。",
                    code="validation_false_no_evidence",
                )
            return answer
        if answer.status == "candidates" and not answer.candidates:
            raise ValidationFailure(code="validation_candidate_missing")
        if answer.status == "candidates" and trace.search_result.hits:
            top_hit = trace.search_result.hits[0]
            top_item = self.repository.get_knowledge(
                workspace_id, top_hit.knowledge_id, top_hit.version
            )
            top_is_applicable = (
                any(fact.kind is FactKind.CHECK_ACTION for fact in top_item.facts)
                and "contradicted" not in top_hit.condition_matches.values()
            )
            first = answer.candidates[0]
            if top_is_applicable and (first.knowledge_id, first.version) != (
                top_hit.knowledge_id,
                top_hit.version,
            ):
                raise ValidationFailure(
                    "適用可能な検索1位の最新版が第1候補に含まれていません。",
                    code="validation_top_candidate_missing",
                )
        acquired = {(hit.knowledge_id, hit.version): hit for hit in trace.search_result.hits}
        for candidate in answer.candidates:
            key = (candidate.knowledge_id, candidate.version)
            if key not in acquired or key not in trace.context_versions:
                raise ValidationFailure(
                    "取得していない知識・版が回答に含まれています。",
                    code="validation_unacquired_knowledge",
                )
            item = self.repository.get_knowledge(
                workspace_id, candidate.knowledge_id, candidate.version
            )
            facts = {fact.id: fact for fact in item.facts}
            action = facts.get(candidate.action_fact_id)
            if action is None or action.kind is not FactKind.CHECK_ACTION:
                raise ValidationFailure(
                    "確認行動ではないfactが候補に選ばれています。",
                    code="validation_action_fact",
                )
            required_conditions = {
                fact.id
                for fact in item.facts
                if fact.kind is FactKind.CONDITION
                and fact.condition_scope
                in {ConditionScope.CASE_CONTEXT, ConditionScope.ACTION_PREREQUISITE}
            }
            if set(candidate.condition_fact_ids) != required_conditions:
                raise ValidationFailure(
                    "現行版の適用条件または行動前提が欠けています。",
                    code="validation_condition_set",
                )
            if not set(candidate.evidence_segment_ids).issubset(trace.evidence_ids):
                raise ValidationFailure(
                    "実際に取得していない原文IDが含まれています。",
                    code="validation_unread_evidence",
                )
            necessary_evidence = {
                ref.segment_id
                for fact in item.facts
                if fact.id in {candidate.action_fact_id, *required_conditions}
                for ref in fact.evidence_refs
            }
            if not necessary_evidence.issubset(set(candidate.evidence_segment_ids)):
                raise ValidationFailure(
                    "候補の行動・条件を支える根拠が欠けています。",
                    code="validation_missing_evidence",
                )
        return answer
