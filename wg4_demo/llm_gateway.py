"""The only path to OpenAI: fixed model, audited ledger, no automatic retries."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar, cast

import tiktoken
from agents import Model, ModelSettings
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem
from agents.models.interface import ModelTracing
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tool import Tool
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
    RateLimitError,
)
from openai.types.responses import ResponsePromptParam, ResponseStreamEvent
from pydantic import BaseModel, ValidationError

from wg4_demo.errors import AppError, ConfigurationError, IndeterminateError, ValidationFailure
from wg4_demo.settings import Settings
from wg4_demo.usage_ledger import CallReservation, UsageLedger

OutputT = TypeVar("OutputT", bound=BaseModel)
ClientFactory = Callable[..., AsyncOpenAI]


@dataclass(frozen=True, slots=True)
class GatewayCallContext:
    session_id: str
    action_id: str
    deadline: datetime


class LLMGateway:
    def __init__(
        self,
        settings: Settings,
        ledger: UsageLedger,
        *,
        client_factory: ClientFactory = AsyncOpenAI,
        encoding_name_override: str | None = None,
        token_estimator: Callable[[str], int] | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.client_factory = client_factory
        self.encoding_name_override = encoding_name_override
        self.token_estimator = token_estimator

    def create_client(self) -> AsyncOpenAI:
        if self.settings.openai_api_key is None:
            raise ConfigurationError("OPENAI_API_KEYが未設定です。")
        return self.client_factory(
            api_key=self.settings.openai_api_key.get_secret_value(),
            timeout=self.settings.request_timeout_seconds,
            max_retries=0,
        )

    async def structured(
        self,
        context: GatewayCallContext,
        *,
        instructions: str,
        input_text: str,
        output_type: type[OutputT],
    ) -> OutputT:
        model = self._required_model()
        payload = {"instructions": instructions, "input": input_text}
        self._validate_prompt(payload)
        client = self.create_client()
        try:
            reservation = await self.ledger.reserve_call_with_wait(
                session_id=context.session_id,
                action_id=context.action_id,
                model=model,
                estimated_input_tokens=self.estimate_tokens(
                    json.dumps(payload, ensure_ascii=False)
                ),
                deadline=context.deadline,
            )
            try:
                response = await client.responses.parse(
                    model=model,
                    instructions=instructions,
                    input=input_text,
                    text_format=output_type,
                    reasoning={"effort": self.settings.openai_reasoning_effort},
                    max_output_tokens=self.settings.max_output_tokens,
                    store=False,
                    parallel_tool_calls=False,
                )
            except BaseException as exc:
                self._record_exception(reservation, exc)
                raise self._safe_error(exc) from exc
            usage = getattr(response, "usage", None)
            self.ledger.finish_call(
                reservation,
                state="completed",
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
            )
            if getattr(response, "status", None) != "completed":
                if getattr(response, "status", None) == "incomplete":
                    raise AppError(
                        "model_incomplete", "モデル出力が未完了のため結果を使用しません。", "llm"
                    )
                raise AppError("model_failed", "モデル処理が完了しませんでした。", "llm")
            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                raise ValidationFailure("refusalまたは構造化出力不成立のため結果を使用しません。")
            if not isinstance(parsed, output_type):
                raise ValidationFailure("構造化出力の型が一致しません。")
            return parsed
        finally:
            await client.close()

    def budgeted_agent_model(
        self, context: GatewayCallContext, client: AsyncOpenAI
    ) -> BudgetedResponsesModel:
        model = self._required_model()
        return BudgetedResponsesModel(
            gateway=self,
            context=context,
            delegate=OpenAIResponsesModel(model=model, openai_client=client),
        )

    def estimate_tokens(self, text: str) -> int:
        if self.token_estimator is not None:
            return self.token_estimator(text)
        try:
            if self.encoding_name_override:
                encoding = tiktoken.get_encoding(self.encoding_name_override)
            else:
                encoding = tiktoken.encoding_for_model(self._required_model())
        except KeyError as exc:
            raise ConfigurationError(
                "選定モデルのtokenizer対応を確認できないためAPIを送信しません。"
            ) from exc
        return len(encoding.encode(text))

    def _validate_prompt(self, payload: object) -> None:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        if len(serialized.encode("utf-8")) > self.settings.max_prompt_bytes:
            raise AppError("prompt_too_large", "送信内容がbyte上限を超えています。", "policy")
        if self.estimate_tokens(serialized) > self.settings.max_estimated_input_tokens:
            raise AppError("prompt_too_large", "送信内容がtoken上限を超えています。", "policy")

    def _required_model(self) -> str:
        if self.settings.openai_model is None:
            raise ConfigurationError("OPENAI_MODELが未設定です。")
        return self.settings.openai_model

    def _record_exception(self, reservation: CallReservation, exc: BaseException) -> None:
        if isinstance(exc, (APITimeoutError, APIConnectionError, TimeoutError)):
            self.ledger.finish_call(
                reservation,
                state="unknown",
                input_tokens=None,
                output_tokens=None,
                safe_error_code="api_state_unknown",
            )
            return
        code = self._provider_code(exc)
        if code in {"project_spend_limit_exceeded", "organization_spend_limit_exceeded"}:
            self.ledger.stop_for_provider_limit(reservation, code)
            return
        self.ledger.finish_call(
            reservation,
            state="failed",
            input_tokens=None,
            output_tokens=None,
            safe_error_code=code or "api_failed",
        )

    def _safe_error(self, exc: BaseException) -> AppError:
        if isinstance(exc, (APITimeoutError, APIConnectionError, TimeoutError)):
            return IndeterminateError(
                "api_state_unknown",
                "API処理または課金の成否を確認できません。自動再送は行いません。",
                "llm",
            )
        code = self._provider_code(exc)
        if code in {"project_spend_limit_exceeded", "organization_spend_limit_exceeded"}:
            return AppError(code, "外部の支出上限に達したためLLM機能を停止しました。", "llm")
        if isinstance(exc, ValidationError):
            return ValidationFailure(
                "構造化出力がschemaに一致しないため結果を使用しません。",
                code="structured_output_invalid",
            )
        if isinstance(exc, LengthFinishReasonError):
            return AppError(
                "model_incomplete",
                "モデル出力が生成上限で未完了のため結果を使用しません。",
                "llm",
            )
        if isinstance(exc, ContentFilterFinishReasonError):
            return ValidationFailure(
                "モデル出力が完了しなかったため結果を使用しません。",
                code="model_content_filtered",
            )
        if isinstance(exc, APIResponseValidationError):
            return AppError(
                "provider_response_invalid",
                "API応答を検証できなかったため結果を使用しません。",
                "llm",
            )
        if isinstance(exc, RateLimitError):
            return AppError("provider_rate_limit", "APIのレート上限に達しました。", "llm")
        if isinstance(exc, APIStatusError):
            return AppError("provider_error", "APIがエラーを返しました。", "llm")
        return AppError("api_failed", "API呼出しに失敗しました。", "llm")

    def _provider_code(self, exc: BaseException) -> str | None:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                return cast(str, error["code"])
        return None


class BudgetedResponsesModel(Model):
    """Agents SDK Model wrapper that reserves every model turn separately."""

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        context: GatewayCallContext,
        delegate: OpenAIResponsesModel,
    ) -> None:
        self.gateway = gateway
        self.context = context
        self.delegate = delegate

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        payload = {
            "system_instructions": system_instructions,
            "input": input,
            "tools": [tool.name for tool in tools],
            "output_schema": str(output_schema),
        }
        self.gateway._validate_prompt(payload)
        reservation = await self.gateway.ledger.reserve_call_with_wait(
            session_id=self.context.session_id,
            action_id=self.context.action_id,
            model=self.gateway._required_model(),
            estimated_input_tokens=self.gateway.estimate_tokens(
                json.dumps(payload, ensure_ascii=False, default=str)
            ),
            deadline=self.context.deadline,
        )
        try:
            response = await self.delegate.get_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            )
        except BaseException as exc:
            self.gateway._record_exception(reservation, exc)
            raise self.gateway._safe_error(exc) from exc
        self.gateway.ledger.finish_call(
            reservation,
            state="completed",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return response

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        del (
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )

        async def disabled() -> AsyncIterator[ResponseStreamEvent]:
            raise RuntimeError("streaming is disabled for this application")
            yield cast(ResponseStreamEvent, None)  # pragma: no cover

        return disabled()
