from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from wg4_demo.agent_runtime import PromptStore, StructuredWorkflowService
from wg4_demo.errors import ValidationFailure
from wg4_demo.llm_gateway import GatewayCallContext
from wg4_demo.repository import Repository
from wg4_demo.schemas import InterviewQuestion, InterviewStatus, InterviewTopic
from wg4_demo.ui.register import _fixed_interview_reply, _parse_interview_outcome


class MockInterviewGateway:
    def __init__(self, result: InterviewQuestion) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def structured(self, context: GatewayCallContext, **kwargs: Any) -> InterviewQuestion:
        self.calls.append({"context": context, **kwargs})
        return self.result


def interview_context() -> GatewayCallContext:
    return GatewayCallContext(
        "interview-session",
        "interview-action",
        datetime.now(UTC) + timedelta(seconds=30),
    )


def test_interview_output_distinguishes_question_from_completion() -> None:
    question = InterviewQuestion(
        status=InterviewStatus.ASK,
        topic=InterviewTopic.DECISION_REASON,
        question="別計器で照合した判断理由は何ですか？",
        related_missing_field="判断理由",
    )
    complete = InterviewQuestion(
        status=InterviewStatus.COMPLETE,
        topic=None,
        question=None,
        related_missing_field=None,
    )

    assert question.question is not None
    assert complete.status is InterviewStatus.COMPLETE
    assert set(InterviewQuestion.model_json_schema()["required"]) == {
        "status",
        "topic",
        "question",
        "related_missing_field",
    }
    with pytest.raises(ValidationError):
        InterviewQuestion(
            status=InterviewStatus.COMPLETE,
            topic=InterviewTopic.EXCEPTION,
            question="さらに質問しますか？",
            related_missing_field=None,
        )


@pytest.mark.asyncio
async def test_interview_rejects_semantically_repeated_topic(
    repository: Repository, project_root: Path
) -> None:
    gateway = MockInterviewGateway(
        InterviewQuestion(
            status=InterviewStatus.ASK,
            topic=InterviewTopic.EXCEPTION,
            question="入口温度が通常範囲外なら参照しませんか？",
            related_missing_field=None,
        )
    )
    service = StructuredWorkflowService(
        gateway,  # type: ignore[arg-type]
        repository,
        PromptStore(project_root / "prompts"),
    )

    with pytest.raises(ValidationFailure) as exc_info:
        await service.interview(
            interview_context(),
            draft={"facts": [], "cause_status": "unresolved", "missing_fields": []},
            statements=[
                {
                    "segment_id": "question-1",
                    "speaker": "assistant",
                    "topic": "exception",
                    "text": "流量低下時にはこの経験を参照しませんか？",
                },
                {"segment_id": "answer-1", "speaker": "operator", "text": "参照しません。"},
            ],
        )

    assert exc_info.value.code == "interview_question_repeated"
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_interview_accepts_explicit_completion(
    repository: Repository, project_root: Path
) -> None:
    expected = InterviewQuestion(
        status=InterviewStatus.COMPLETE,
        topic=None,
        question=None,
        related_missing_field=None,
    )
    gateway = MockInterviewGateway(expected)
    service = StructuredWorkflowService(
        gateway,  # type: ignore[arg-type]
        repository,
        PromptStore(project_root / "prompts"),
    )

    actual = await service.interview(
        interview_context(),
        draft={"facts": [], "cause_status": "unresolved", "missing_fields": []},
        statements=[],
    )

    assert actual == expected


def test_fixed_interview_examples_match_topic_and_are_used_only_once(project_root: Path) -> None:
    demo = json.loads((project_root / "data" / "demo_inputs_v5.json").read_text("utf-8"))

    assert (
        _fixed_interview_reply(demo, InterviewTopic.DECISION_REASON, set())
        == (demo["interview"]["reason_reply"])
    )
    assert (
        _fixed_interview_reply(demo, InterviewTopic.APPLICABILITY, set())
        == (demo["interview"]["scope_reply"])
    )
    assert (
        _fixed_interview_reply(
            demo,
            InterviewTopic.EXCEPTION,
            {InterviewTopic.APPLICABILITY},
        )
        == ""
    )
    assert (
        _fixed_interview_reply(
            demo,
            InterviewTopic.DECISION_REASON,
            {InterviewTopic.DECISION_REASON},
        )
        == ""
    )


def test_legacy_question_can_be_displayed_without_treating_it_as_new_output() -> None:
    parsed = _parse_interview_outcome(
        {
            "question": "以前の版で保存された質問ですか？",
            "related_missing_field": None,
        }
    )

    assert parsed.status is InterviewStatus.ASK
    assert parsed.topic is InterviewTopic.OTHER


def test_stored_structured_question_restores_strict_enums_from_json() -> None:
    parsed = _parse_interview_outcome(
        {
            "status": "ask",
            "topic": "decision_reason",
            "question": "別の計器で確認した判断理由は何ですか？",
            "related_missing_field": "判断理由",
        }
    )

    assert parsed.status is InterviewStatus.ASK
    assert parsed.topic is InterviewTopic.DECISION_REASON
