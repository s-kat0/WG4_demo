"""Single-agent workflows and fixed structured-output LLM workflows."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from agents import (
    Agent,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    ModelRetrySettings,
    ModelSettings,
    RunConfig,
    Runner,
    set_tracing_disabled,
)
from agents.tool import Tool
from openai.types.shared import Reasoning

from wg4_demo.errors import ValidationFailure
from wg4_demo.evidence import EvidenceService
from wg4_demo.graph import GraphService
from wg4_demo.llm_gateway import GatewayCallContext, LLMGateway
from wg4_demo.repository import ProposalRecord, Repository
from wg4_demo.result_validation import ResultValidator
from wg4_demo.retrieval import RetrievalService
from wg4_demo.schemas import (
    AnswerSelection,
    CauseStatus,
    ConditionScope,
    FactKind,
    InterviewQuestion,
    InterviewStatus,
    InterviewTopic,
    KnowledgeDraft,
    OperationType,
    ProposalOperation,
    ProposalSelection,
)
from wg4_demo.settings import Settings
from wg4_demo.tools import ALL_TOOLS, QA_TOOLS, ToolRuntimeContext

set_tracing_disabled(True)


def translate_agent_error(exc: BaseException) -> ValidationFailure:
    if isinstance(exc, ModelRefusalError):
        return ValidationFailure(
            "モデルが回答を拒否したため結果を使用しません。",
            code="agent_refusal",
        )
    if isinstance(exc, MaxTurnsExceeded):
        return ValidationFailure(
            "モデルの最大ターン数に達したため結果を使用しません。",
            code="agent_turn_limit",
        )
    if isinstance(exc, ModelBehaviorError):
        message = str(exc)
        if message.startswith("Invalid JSON input for tool"):
            code = "agent_tool_input_invalid"
        elif "final output" in message or "parsing model output" in message:
            code = "agent_output_invalid"
        else:
            code = "agent_model_behavior"
        return ValidationFailure(
            "モデル出力を検証できなかったため結果を使用しません。",
            code=code,
        )
    raise TypeError("unsupported agent error")


class PromptStore:
    def __init__(self, prompt_dir: Path) -> None:
        self.prompt_dir = prompt_dir

    def read(self, name: str) -> str:
        return (self.prompt_dir / f"{name}.md").read_text(encoding="utf-8")


class StructuredWorkflowService:
    def __init__(
        self,
        gateway: LLMGateway,
        repository: Repository,
        prompts: PromptStore,
    ) -> None:
        self.gateway = gateway
        self.repository = repository
        self.prompts = prompts

    async def extract(
        self,
        context: GatewayCallContext,
        *,
        segments: list[dict[str, str]],
    ) -> KnowledgeDraft:
        draft = await self.gateway.structured(
            context,
            instructions=self.prompts.read("extraction"),
            input_text=json.dumps({"segments": segments}, ensure_ascii=False),
            output_type=KnowledgeDraft,
        )
        self._validate_extraction_result(draft)
        return draft

    @staticmethod
    def _validate_extraction_result(draft: KnowledgeDraft) -> None:
        """Reject semantically incomplete extraction output without repairing it."""

        allowed_kinds = {
            FactKind.OBSERVATION,
            FactKind.CHECK_ACTION,
            FactKind.CONDITION,
            FactKind.CAUSE_STATUS,
        }
        if any(fact.kind not in allowed_kinds for fact in draft.facts):
            raise ValidationFailure(
                "文書抽出で許可されていないfact種別が含まれるため使用しません。",
                code="extraction_fact_kind_invalid",
            )
        if any(
            fact.kind is FactKind.CONDITION
            and fact.condition_scope not in {ConditionScope.CASE_CONTEXT, ConditionScope.EXCLUSION}
            for fact in draft.facts
        ):
            raise ValidationFailure(
                "文書抽出の条件種別を検証できないため使用しません。",
                code="extraction_condition_scope_invalid",
            )

        missing = "\n".join(draft.missing_fields)
        absent_categories: list[str] = []
        if draft.cause_status is not CauseStatus.CONFIRMED_IN_SOURCE and "原因" not in missing:
            absent_categories.append("原因")

        has_check_action = any(fact.kind is FactKind.CHECK_ACTION for fact in draft.facts)
        if has_check_action:
            if not any(term in missing for term in ("判断理由", "理由")):
                absent_categories.append("判断理由")

            if not any(term in missing for term in ("適用範囲", "一般化", "例外")):
                absent_categories.append("適用範囲")

        if absent_categories:
            raise ValidationFailure(
                "抽出結果が原文にない情報の不足を明示していないため使用しません。",
                code="extraction_missing_fields_invalid",
            )

    async def interview(
        self,
        context: GatewayCallContext,
        *,
        draft: dict[str, Any],
        statements: list[dict[str, str]],
    ) -> InterviewQuestion:
        result = await self.gateway.structured(
            context,
            instructions=self.prompts.read("interview"),
            input_text=json.dumps(
                {"current_draft": draft, "statements": statements}, ensure_ascii=False
            ),
            output_type=InterviewQuestion,
        )
        self._validate_interview_result(result, statements)
        return result

    @staticmethod
    def _validate_interview_result(
        result: InterviewQuestion, statements: list[dict[str, str]]
    ) -> None:
        if result.status is InterviewStatus.COMPLETE:
            return
        if result.topic is None or result.question is None:
            raise ValidationFailure(
                "追加質問の形式を検証できません。",
                code="interview_question_invalid",
            )
        prior_topics: set[InterviewTopic] = set()
        prior_questions: set[str] = set()
        for statement in statements:
            if statement.get("speaker") != "assistant":
                continue
            question = statement.get("text")
            if question:
                prior_questions.add("".join(question.split()).rstrip("?？。"))
            raw_topic = statement.get("topic")
            if raw_topic:
                try:
                    prior_topics.add(InterviewTopic(raw_topic))
                except ValueError as exc:
                    raise ValidationFailure(
                        "保存済みの聞き取りトピックを検証できません。",
                        code="interview_topic_invalid",
                    ) from exc
        normalized = "".join(result.question.split()).rstrip("?？。")
        if result.topic in prior_topics or normalized in prior_questions:
            raise ValidationFailure(
                "既に確認した内容と重複する追加質問だったため表示しません。",
                code="interview_question_repeated",
            )

    async def reflect_and_stage(
        self,
        context: GatewayCallContext,
        *,
        workspace_id: str,
        segments: list[dict[str, str]],
        allowed_segment_ids: set[str],
        equipment: str,
        case_label: str | None,
    ) -> ProposalRecord:
        draft = await self.extract(context, segments=segments)
        operations = [
            ProposalOperation(operation=OperationType.ADD_FACT, new_fact=fact)
            for fact in draft.facts
        ]
        return self.repository.stage_proposal(
            workspace_id,
            action_id=context.action_id,
            target_item_id=None,
            base_version=0,
            operations=operations,
            reason="文書と模擬聞き取りの内容を確認済み知識候補として整理",
            equipment=equipment,
            case_label=case_label,
            missing_fields=draft.missing_fields,
            cause_status=CauseStatus(draft.cause_status),
            allowed_segment_ids=allowed_segment_ids,
        )

    async def supplement_and_stage(
        self,
        context: GatewayCallContext,
        *,
        workspace_id: str,
        target_item_id: str,
        target_version: int,
        statements: list[dict[str, str]],
        allowed_segment_ids: set[str],
    ) -> ProposalRecord:
        """Extract only human-provided supplements for an existing approved item."""

        item = self.repository.get_knowledge(workspace_id, target_item_id, target_version)
        draft = await self.gateway.structured(
            context,
            instructions=self.prompts.read("interview_update"),
            input_text=json.dumps(
                {
                    "current_knowledge": {
                        "knowledge_id": item.id,
                        "version": item.version,
                        "facts": [fact.model_dump(mode="json") for fact in item.facts],
                        "missing_fields": item.missing_fields,
                        "cause_status": item.cause_status.value,
                    },
                    "statements": statements,
                },
                ensure_ascii=False,
            ),
            output_type=KnowledgeDraft,
        )
        existing = {(fact.kind, fact.text) for fact in item.facts}
        new_facts = [fact for fact in draft.facts if (fact.kind, fact.text) not in existing]
        if not new_facts:
            raise ValidationFailure(
                "本人役の発言から新しい根拠付きfactを抽出できませんでした。",
                code="supplement_no_new_fact",
            )
        operations = [
            ProposalOperation(operation=OperationType.ADD_FACT, new_fact=fact) for fact in new_facts
        ]
        return self.repository.stage_proposal(
            workspace_id,
            action_id=context.action_id,
            target_item_id=item.id,
            base_version=item.version,
            operations=operations,
            reason="本人役への聞き取りで判断理由・適用範囲を補足",
            equipment=item.equipment,
            case_label=item.case_label,
            title=item.title,
            missing_fields=draft.missing_fields,
            cause_status=item.cause_status,
            allowed_segment_ids=allowed_segment_ids,
        )


class AgentService:
    def __init__(
        self,
        settings: Settings,
        gateway: LLMGateway,
        repository: Repository,
        retrieval: RetrievalService,
        graph: GraphService,
        evidence: EvidenceService,
        validator: ResultValidator,
        prompts: PromptStore,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.repository = repository
        self.retrieval = retrieval
        self.graph = graph
        self.evidence = evidence
        self.validator = validator
        self.prompts = prompts

    async def answer(
        self,
        call_context: GatewayCallContext,
        tool_context: ToolRuntimeContext,
        question: str,
        *,
        conversation_context: dict[str, Any] | None = None,
    ) -> tuple[AnswerSelection, ToolRuntimeContext]:
        client = self.gateway.create_client()
        try:
            agent = Agent[ToolRuntimeContext](
                name="WG4 knowledge evidence agent",
                instructions=self.prompts.read("answer"),
                tools=cast(list[Tool], QA_TOOLS),
                model=self.gateway.budgeted_agent_model(call_context, client),
                model_settings=self._model_settings(),
                output_type=AnswerSelection,
            )
            result = await Runner.run(
                agent,
                json.dumps(
                    {
                        "question": question,
                        "consultation": conversation_context or {},
                    },
                    ensure_ascii=False,
                ),
                context=tool_context,
                max_turns=self.settings.max_model_calls_per_action,
                run_config=self._run_config(),
            )
            answer = (
                result.final_output
                if isinstance(result.final_output, AnswerSelection)
                else AnswerSelection.model_validate(result.final_output)
            )
            return self.validator.validate_answer(
                tool_context.workspace_id,
                answer,
                tool_context.trace,
                expected_intent=tool_context.answer_intent,
                focus_knowledge_ids=tool_context.focus_knowledge_ids,
                explicit_focus=tool_context.explicit_focus,
            ), tool_context
        except (MaxTurnsExceeded, ModelBehaviorError, ModelRefusalError) as exc:
            raise translate_agent_error(exc) from exc
        finally:
            await client.close()

    async def propose_update(
        self,
        call_context: GatewayCallContext,
        tool_context: ToolRuntimeContext,
        statement: str,
    ) -> tuple[ProposalRecord, ToolRuntimeContext]:
        client = self.gateway.create_client()
        try:
            agent = Agent[ToolRuntimeContext](
                name="WG4 knowledge update agent",
                instructions=self.prompts.read("update"),
                tools=cast(list[Tool], ALL_TOOLS),
                model=self.gateway.budgeted_agent_model(call_context, client),
                model_settings=self._model_settings(),
                output_type=ProposalSelection,
            )
            result = await Runner.run(
                agent,
                statement,
                context=tool_context,
                max_turns=self.settings.max_model_calls_per_action,
                run_config=self._run_config(),
            )
            selection = (
                result.final_output
                if isinstance(result.final_output, ProposalSelection)
                else ProposalSelection.model_validate(result.final_output)
            )
            if (
                tool_context.staged_proposal_id is None
                or selection.proposal_id != tool_context.staged_proposal_id
            ):
                raise ValidationFailure("検証済みのstaged proposalと出力が一致しません。")
            return self.repository.get_proposal(
                tool_context.workspace_id, selection.proposal_id
            ), tool_context
        except (MaxTurnsExceeded, ModelBehaviorError, ModelRefusalError) as exc:
            raise translate_agent_error(exc) from exc
        finally:
            await client.close()

    def new_tool_context(
        self,
        *,
        session_id: str,
        workspace_id: str,
        action_id: str,
        mode: str,
        kb_revision: int,
        deadline: datetime,
        submitted_segment_ids: set[str] | None = None,
        answer_intent: str = "candidate_search",
        focus_knowledge_ids: set[str] | None = None,
        explicit_focus: bool = False,
        is_active: Callable[[], bool] = lambda: True,
    ) -> ToolRuntimeContext:
        return ToolRuntimeContext(
            session_id=session_id,
            workspace_id=workspace_id,
            action_id=action_id,
            mode=mode,
            kb_revision=kb_revision,
            deadline=deadline,
            repository=self.repository,
            retrieval=self.retrieval,
            graph=self.graph,
            evidence=self.evidence,
            max_tool_calls=self.settings.max_tool_calls_per_action,
            submitted_segment_ids=submitted_segment_ids or set(),
            answer_intent=answer_intent,
            focus_knowledge_ids=focus_knowledge_ids or set(),
            explicit_focus=explicit_focus,
            is_active=is_active,
        )

    def _model_settings(self) -> ModelSettings:
        return ModelSettings(
            parallel_tool_calls=False,
            max_tokens=self.settings.max_output_tokens,
            reasoning=Reasoning(effort=self.settings.openai_reasoning_effort),
            store=False,
            retry=ModelRetrySettings(max_retries=0),
            timeout=float(self.settings.request_timeout_seconds),
        )

    def _run_config(self) -> RunConfig:
        return RunConfig(
            tracing_disabled=True,
            trace_include_sensitive_data=False,
            tool_not_found_behavior="raise_error",
        )
