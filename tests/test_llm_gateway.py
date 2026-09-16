from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from wg4_demo.auth import AuthService
from wg4_demo.errors import IndeterminateError, ValidationFailure
from wg4_demo.llm_gateway import GatewayCallContext, LLMGateway
from wg4_demo.schemas import (
    CauseStatus,
    EvidenceDraft,
    FactDraft,
    FactKind,
    KnowledgeDraft,
    Role,
    SessionRecord,
)
from wg4_demo.settings import Settings
from wg4_demo.usage_ledger import UsageLedger


class FakeResponses:
    def __init__(self, owner) -> None:
        self.owner = owner

    async def parse(self, **kwargs):
        self.owner.parse_calls.append(kwargs)
        if self.owner.error is not None:
            raise self.owner.error
        return self.owner.response


class FakeClient:
    def __init__(self, *, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.parse_calls: list[dict[str, object]] = []
        self.responses = FakeResponses(self)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.kwargs: dict[str, object] | None = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.client


def enabled_ledger(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> UsageLedger:
    ledger = UsageLedger(settings.control_db_path, settings, auth)
    admin = auth.login("admin-secret", role=Role.ADMIN, client_token="gateway-admin")
    ledger.enable_budget(
        admin.id,
        additional_calls=10,
        confirmed_external_limit=True,
        reason="gateway tests",
    )
    return ledger


def draft() -> KnowledgeDraft:
    return KnowledgeDraft(
        facts=[
            FactDraft(
                kind=FactKind.OBSERVATION,
                text="出口温度の表示が上昇",
                evidence=[EvidenceDraft(segment_id="segment-1", quote="表示が上昇")],
            )
        ],
        cause_status=CauseStatus.UNRESOLVED,
        missing_fields=["原因"],
    )


@pytest.mark.asyncio
async def test_payload_has_fixed_model_store_false_and_no_client_retry(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    expected = draft()
    client = FakeClient(
        response=SimpleNamespace(
            status="completed",
            output_parsed=expected,
            usage=SimpleNamespace(input_tokens=12, output_tokens=5),
        )
    )
    factory = FakeFactory(client)
    gateway = LLMGateway(
        settings,
        enabled_ledger(settings, auth, participant),
        client_factory=factory,
        token_estimator=lambda text: len(text),
    )
    context = GatewayCallContext(
        participant.id, "gateway-action", datetime.now(UTC) + timedelta(seconds=10)
    )

    actual = await gateway.structured(
        context,
        instructions="extract strictly",
        input_text="input",
        output_type=KnowledgeDraft,
    )

    assert actual == expected
    assert factory.kwargs is not None
    assert factory.kwargs["max_retries"] == 0
    assert client.parse_calls == [
        {
            "model": "test-model",
            "instructions": "extract strictly",
            "input": "input",
            "text_format": KnowledgeDraft,
            "reasoning": {"effort": "low"},
            "max_output_tokens": 2048,
            "store": False,
            "parallel_tool_calls": False,
        }
    ]
    assert client.closed is True


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_not_retried(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    client = FakeClient(error=TimeoutError("provider state unknown"))
    gateway = LLMGateway(
        settings,
        enabled_ledger(settings, auth, participant),
        client_factory=FakeFactory(client),
        token_estimator=lambda text: len(text),
    )

    with pytest.raises(IndeterminateError, match="api_state_unknown"):
        await gateway.structured(
            GatewayCallContext(
                participant.id, "timeout-action", datetime.now(UTC) + timedelta(seconds=10)
            ),
            instructions="extract",
            input_text="input",
            output_type=KnowledgeDraft,
        )

    assert len(client.parse_calls) == 1
    status = gateway.ledger.status()
    assert status["used_calls"] == 1
    assert status["active_calls"] == 0


@pytest.mark.asyncio
async def test_missing_parsed_output_is_not_repaired(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    client = FakeClient(
        response=SimpleNamespace(
            status="completed",
            output_parsed=None,
            usage=SimpleNamespace(input_tokens=4, output_tokens=1),
        )
    )
    gateway = LLMGateway(
        settings,
        enabled_ledger(settings, auth, participant),
        client_factory=FakeFactory(client),
        token_estimator=lambda text: len(text),
    )

    with pytest.raises(ValidationFailure):
        await gateway.structured(
            GatewayCallContext(
                participant.id, "invalid-output", datetime.now(UTC) + timedelta(seconds=10)
            ),
            instructions="extract",
            input_text="input",
            output_type=KnowledgeDraft,
        )
    assert len(client.parse_calls) == 1


@pytest.mark.asyncio
async def test_pydantic_parse_failure_is_a_validation_error(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    with pytest.raises(ValidationError) as invalid:
        KnowledgeDraft.model_validate({})
    client = FakeClient(error=invalid.value)
    gateway = LLMGateway(
        settings,
        enabled_ledger(settings, auth, participant),
        client_factory=FakeFactory(client),
        token_estimator=lambda text: len(text),
    )

    with pytest.raises(ValidationFailure) as exc_info:
        await gateway.structured(
            GatewayCallContext(
                participant.id,
                "pydantic-invalid-output",
                datetime.now(UTC) + timedelta(seconds=10),
            ),
            instructions="extract",
            input_text="input",
            output_type=KnowledgeDraft,
        )

    assert exc_info.value.code == "structured_output_invalid"
    assert len(client.parse_calls) == 1
