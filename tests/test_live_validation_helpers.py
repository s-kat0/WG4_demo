from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.run_live_validation import validate_v5_interview_supplement
from wg4_demo.schemas import ConditionScope, EvidenceRef, FactKind, StoredFact


def stored_fact(
    fact_id: str,
    kind: FactKind,
    segment_id: str,
    *,
    condition_scope: ConditionScope | None = None,
) -> StoredFact:
    return StoredFact(
        id=fact_id,
        kind=kind,
        text=fact_id,
        condition_scope=condition_scope,
        parent_action_fact_id=None,
        evidence_refs=[
            EvidenceRef(
                segment_id=segment_id,
                quote="根拠",
                start_char=0,
                end_char=2,
            )
        ],
    )


def test_live_interview_validation_uses_grounded_outcomes_not_question_wording() -> None:
    item = SimpleNamespace(
        facts=[
            stored_fact("reason", FactKind.DECISION_REASON, "reason-segment"),
            stored_fact(
                "scope",
                FactKind.CONDITION,
                "scope-segment",
                condition_scope=ConditionScope.APPLICABILITY,
            ),
        ]
    )

    validate_v5_interview_supplement(
        item,
        reason_segment_id="reason-segment",
        scope_segment_id="scope-segment",
    )


def test_live_interview_validation_rejects_missing_grounded_scope() -> None:
    item = SimpleNamespace(
        facts=[stored_fact("reason", FactKind.DECISION_REASON, "reason-segment")]
    )

    with pytest.raises(RuntimeError, match="decision reason or applicability scope"):
        validate_v5_interview_supplement(
            item,
            reason_segment_id="reason-segment",
            scope_segment_id="scope-segment",
        )
