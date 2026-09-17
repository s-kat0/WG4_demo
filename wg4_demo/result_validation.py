"""Validate agent selections against actually executed tool results."""

from __future__ import annotations

from dataclasses import dataclass, field

from wg4_demo.errors import ValidationFailure
from wg4_demo.repository import Repository
from wg4_demo.schemas import AnswerSelection, ConditionScope, FactKind, SearchSuccess, StoredFact


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
        self,
        workspace_id: str,
        answer: AnswerSelection,
        trace: ToolTrace,
        *,
        expected_intent: str = "candidate_search",
        focus_knowledge_ids: set[str] | None = None,
        explicit_focus: bool = False,
    ) -> AnswerSelection:
        if answer.intent != expected_intent:
            raise ValidationFailure(
                "質問意図と回答形式が一致しません。",
                code="validation_answer_intent",
            )
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
        focus = focus_knowledge_ids or set()
        if (
            answer.status == "candidates"
            and expected_intent == "candidate_search"
            and explicit_focus
            and focus
            and answer.candidates[0].knowledge_id not in focus
        ):
            raise ValidationFailure(
                "明示的に選択された知識項目と回答対象が一致しません。",
                code="validation_focus_candidate_missing",
            )
        if (
            answer.status == "candidates"
            and trace.search_result.hits
            and expected_intent == "candidate_search"
            and not (explicit_focus and focus)
        ):
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
        if (
            answer.status == "candidates"
            and expected_intent in {"reason_explanation", "evidence_lookup"}
            and focus
            and answer.candidates[0].knowledge_id not in focus
        ):
            raise ValidationFailure(
                "追質問で指定された知識項目と回答対象が一致しません。",
                code="validation_focus_candidate_missing",
            )
        acquired = {(hit.knowledge_id, hit.version): hit for hit in trace.search_result.hits}
        context_facts: dict[str, StoredFact] = {}
        for key in trace.context_versions:
            if key not in acquired:
                raise ValidationFailure(
                    "検索で取得していない知識が探索履歴に含まれています。",
                    code="validation_unsearched_context",
                )
            context_item = self.repository.get_knowledge(workspace_id, *key)
            context_facts.update({fact.id: fact for fact in context_item.facts})
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
            if len(candidate.condition_fact_ids) != len(set(candidate.condition_fact_ids)):
                raise ValidationFailure(
                    "同じ条件factを重複指定できません。",
                    code="validation_duplicate_condition",
                )
            if len(candidate.supporting_fact_ids) != len(set(candidate.supporting_fact_ids)):
                raise ValidationFailure(
                    "同じ補足factを重複指定できません。",
                    code="validation_duplicate_supporting_fact",
                )
            if len(candidate.evidence_segment_ids) != len(set(candidate.evidence_segment_ids)):
                raise ValidationFailure(
                    "同じ原文IDを重複指定できません。",
                    code="validation_duplicate_evidence",
                )
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
                in {
                    ConditionScope.CASE_CONTEXT,
                    ConditionScope.APPLICABILITY,
                    ConditionScope.ACTION_PREREQUISITE,
                }
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
            version_evidence = {ref.segment_id for fact in item.facts for ref in fact.evidence_refs}
            if not set(candidate.evidence_segment_ids).issubset(version_evidence):
                raise ValidationFailure(
                    "別の知識項目の原文が候補へ混入しています。",
                    code="validation_cross_item_evidence",
                )
            supporting = set(candidate.supporting_fact_ids)
            if not supporting.issubset(facts):
                raise ValidationFailure(
                    "取得した知識版にない補足factが含まれています。",
                    code="validation_supporting_fact",
                )
            allowed_supporting_kinds = {
                FactKind.OBSERVATION,
                FactKind.CAUSE_HYPOTHESIS,
                FactKind.DECISION_REASON,
                FactKind.EXCEPTION,
                FactKind.CAUSE_STATUS,
            }
            if any(facts[fact_id].kind not in allowed_supporting_kinds for fact_id in supporting):
                raise ValidationFailure(
                    "条件または確認行動を補足factとして指定できません。",
                    code="validation_supporting_fact_kind",
                )
            necessary_evidence = {
                ref.segment_id
                for fact in item.facts
                if fact.id in {candidate.action_fact_id, *required_conditions, *supporting}
                for ref in fact.evidence_refs
            }
            if not necessary_evidence.issubset(set(candidate.evidence_segment_ids)):
                raise ValidationFailure(
                    "候補の行動・条件を支える根拠が欠けています。",
                    code="validation_missing_evidence",
                )
        conflict_ids = set(answer.conflicting_fact_ids)
        if len(conflict_ids) != len(answer.conflicting_fact_ids):
            raise ValidationFailure(
                "矛盾factを重複指定できません。",
                code="validation_duplicate_conflict",
            )
        if answer.status == "conflict" and len(conflict_ids) < 2:
            raise ValidationFailure(
                "矛盾を示す取得済みfactが不足しています。",
                code="validation_conflict_missing",
            )
        if not conflict_ids.issubset(context_facts):
            raise ValidationFailure(
                "取得していないfactが矛盾情報に含まれています。",
                code="validation_unacquired_conflict",
            )
        for request in answer.clarification_requests:
            condition_id = request.related_condition_id
            if condition_id is None:
                continue
            fact = context_facts.get(condition_id)
            if fact is None or fact.kind is not FactKind.CONDITION:
                raise ValidationFailure(
                    "確認質問が取得済み条件factを参照していません。",
                    code="validation_clarification_condition",
                )
        return answer
