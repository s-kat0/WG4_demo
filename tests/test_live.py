from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from wg4_demo.auth import AuthService
from wg4_demo.llm_gateway import GatewayCallContext, LLMGateway
from wg4_demo.schemas import Role
from wg4_demo.settings import settings_from_environment
from wg4_demo.usage_ledger import UsageLedger


class LiveConnectivityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["ok"]


@pytest.mark.live
@pytest.mark.asyncio
async def test_explicit_gated_live_connectivity(project_root: Path) -> None:
    if os.environ.get("RUN_LIVE_TESTS") != "1":
        pytest.skip("RUN_LIVE_TESTS=1 was not explicitly set")
    password = os.environ.get("LIVE_TEST_PARTICIPANT_PASSWORD")
    assert password, "LIVE_TEST_PARTICIPANT_PASSWORD is required for the gated live test"
    settings = settings_from_environment(runtime_dir=project_root / "runtime")
    assert not settings.live_blockers(now=datetime.now(UTC))
    auth = AuthService(settings.control_db_path, settings)
    ledger = UsageLedger(settings.control_db_path, settings, auth)
    assert ledger.status()["enabled"] is True
    session = auth.login(
        password,
        role=Role.PARTICIPANT,
        client_token=f"live-test-{uuid4()}",
    )
    gateway = LLMGateway(settings, ledger)

    result = await gateway.structured(
        GatewayCallContext(
            session.id, f"live-{uuid4()}", datetime.now(UTC) + timedelta(seconds=60)
        ),
        instructions="Return only the requested structured status.",
        input_text="Return status ok.",
        output_type=LiveConnectivityResult,
    )

    assert result.status == "ok"
