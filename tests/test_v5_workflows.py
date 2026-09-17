from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wg4_demo.agent_runtime import PromptStore, StructuredWorkflowService
from wg4_demo.errors import ValidationFailure
from wg4_demo.llm_gateway import GatewayCallContext
from wg4_demo.repository import Repository
from wg4_demo.schemas import (
    CauseStatus,
    ConditionScope,
    EvidenceDraft,
    FactDraft,
    FactKind,
    KnowledgeDraft,
    SessionRecord,
)


class MockStructuredGateway:
    def __init__(self, result: KnowledgeDraft) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def structured(self, context: GatewayCallContext, **kwargs: Any) -> KnowledgeDraft:
        self.calls.append({"context": context, **kwargs})
        return self.result


def extraction_draft(segment_id: str, *, missing_fields: list[str]) -> KnowledgeDraft:
    return KnowledgeDraft(
        facts=[
            FactDraft(
                kind=FactKind.CHECK_ACTION,
                text="別の計器と照合",
                evidence=[EvidenceDraft(segment_id=segment_id, quote="別の計器とも突き合わせた")],
            )
        ],
        cause_status=CauseStatus.UNRESOLVED,
        missing_fields=missing_fields,
    )


@pytest.mark.asyncio
async def test_extraction_rejects_unreported_missing_information(project_root: Path) -> None:
    gateway = MockStructuredGateway(extraction_draft("segment-1", missing_fields=[]))
    service = StructuredWorkflowService(
        gateway,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        PromptStore(project_root / "prompts"),
    )

    with pytest.raises(ValidationFailure) as caught:
        await service.extract(
            GatewayCallContext(
                "session-1",
                "extract-invalid-missing-fields",
                datetime.now(UTC) + timedelta(seconds=30),
            ),
            segments=[{"segment_id": "segment-1", "text": "別の計器とも突き合わせた"}],
        )

    assert caught.value.code == "extraction_missing_fields_invalid"
    assert gateway.calls and len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_extraction_accepts_explicit_missing_information(project_root: Path) -> None:
    expected = extraction_draft("segment-1", missing_fields=["原因", "判断理由", "適用範囲"])
    gateway = MockStructuredGateway(expected)
    service = StructuredWorkflowService(
        gateway,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        PromptStore(project_root / "prompts"),
    )

    actual = await service.extract(
        GatewayCallContext(
            "session-1",
            "extract-valid-missing-fields",
            datetime.now(UTC) + timedelta(seconds=30),
        ),
        segments=[{"segment_id": "segment-1", "text": "別の計器とも突き合わせた"}],
    )

    assert actual == expected


@pytest.mark.asyncio
async def test_extraction_rejects_decision_reason_fact(project_root: Path) -> None:
    invalid = KnowledgeDraft(
        facts=[
            FactDraft(
                kind=FactKind.CHECK_ACTION,
                text="別の計器と照合",
                evidence=[EvidenceDraft(segment_id="segment-1", quote="突き合わせた")],
            ),
            FactDraft(
                kind=FactKind.DECISION_REASON,
                text="念のため確認",
                evidence=[EvidenceDraft(segment_id="segment-1", quote="念のため")],
            ),
        ],
        cause_status=CauseStatus.UNRESOLVED,
        missing_fields=["原因", "適用範囲"],
    )
    gateway = MockStructuredGateway(invalid)
    service = StructuredWorkflowService(
        gateway,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        PromptStore(project_root / "prompts"),
    )

    with pytest.raises(ValidationFailure) as caught:
        await service.extract(
            GatewayCallContext(
                "session-1",
                "extract-invalid-kind",
                datetime.now(UTC) + timedelta(seconds=30),
            ),
            segments=[{"segment_id": "segment-1", "text": "念のため突き合わせた"}],
        )

    assert caught.value.code == "extraction_fact_kind_invalid"


@pytest.mark.asyncio
async def test_interview_supplement_uses_mock_gateway_and_stays_staged(
    repository: Repository,
    participant: SessionRecord,
    project_root: Path,
) -> None:
    workspace = repository.create_workspace(
        participant.id,
        seed_mode="approved_v1",
        seed_path=project_root / "data" / "approved_seed.json",
    )
    item = repository.list_knowledge(workspace.id)[0]
    _, mapping = repository.register_source(
        workspace.id,
        title="模擬聞き取り",
        kind="interview",
        equipment=item.equipment,
        case_label=item.case_label,
        segments=[
            (
                "mock-reason",
                "operator",
                "同じ時間帯の変化かを区別するため、時刻をそろえて確認します。",
            )
        ],
    )
    segment_id = mapping["mock-reason"]
    gateway = MockStructuredGateway(
        KnowledgeDraft(
            facts=[
                FactDraft(
                    kind=FactKind.DECISION_REASON,
                    text="同じ時間帯の変化かを区別するため",
                    evidence=[
                        EvidenceDraft(
                            segment_id=segment_id,
                            quote="同じ時間帯の変化かを区別するため",
                        )
                    ],
                ),
                FactDraft(
                    kind=FactKind.CONDITION,
                    text="時刻をそろえて比較できる場合",
                    condition_scope=ConditionScope.APPLICABILITY,
                    evidence=[
                        EvidenceDraft(
                            segment_id=segment_id,
                            quote="時刻をそろえて確認します",
                        )
                    ],
                ),
            ],
            cause_status=CauseStatus.UNRESOLVED,
            missing_fields=["例外"],
        )
    )
    service = StructuredWorkflowService(
        gateway,  # type: ignore[arg-type]
        repository,
        PromptStore(project_root / "prompts"),
    )

    proposal = await service.supplement_and_stage(
        GatewayCallContext(
            participant.id,
            "mock-supplement-action",
            datetime.now(UTC) + timedelta(seconds=30),
        ),
        workspace_id=workspace.id,
        target_item_id=item.id,
        target_version=item.version,
        statements=[
            {
                "segment_id": segment_id,
                "speaker": "operator",
                "text": "同じ時間帯の変化かを区別するため、時刻をそろえて確認します。",
            }
        ],
        allowed_segment_ids={segment_id},
    )

    assert proposal.status == "staged"
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["output_type"] is KnowledgeDraft
    assert segment_id not in repository.allowed_evidence_ids(workspace.id)
    assert repository.get_knowledge(workspace.id, item.id).version == 1


def test_prompts_do_not_contain_unsent_demo_replies(project_root: Path) -> None:
    prompt_text = "\n".join(
        path.read_text("utf-8") for path in (project_root / "prompts").glob("*.md")
    )
    assert "表示の変化なのか実際の温度の変化なのかを切り分けたい" not in prompt_text
    assert "照合用の計器も、校正が有効か確認する必要があります" not in prompt_text
