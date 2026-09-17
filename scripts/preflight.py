"""Non-billable configuration and fixture checks by default."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from getpass import getpass
from pathlib import Path
from typing import Literal
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, ConfigDict

from wg4_demo.auth import AuthService
from wg4_demo.llm_gateway import GatewayCallContext, LLMGateway
from wg4_demo.schemas import Role
from wg4_demo.settings import settings_from_environment
from wg4_demo.usage_ledger import UsageLedger


class ConnectivityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["ok"]


async def live_check() -> None:
    if os.environ.get("RUN_LIVE_TESTS") != "1":
        raise RuntimeError("--live also requires RUN_LIVE_TESTS=1")
    root = Path(__file__).resolve().parents[1]
    settings = settings_from_environment(runtime_dir=root / "runtime")
    blockers = settings.live_blockers(now=datetime.now(UTC))
    if blockers:
        raise RuntimeError("live configuration is incomplete")
    auth = AuthService(settings.control_db_path, settings)
    ledger = UsageLedger(settings.control_db_path, settings, auth)
    if not ledger.status()["enabled"]:
        raise RuntimeError(
            "usage ledger is disabled; an administrator must confirm the external hard limit "
            "and resume LLM requests"
        )
    session = auth.login(
        getpass("Participant password for gated live check: "),
        role=Role.PARTICIPANT,
        client_token=f"preflight-{uuid4()}",
    )
    gateway = LLMGateway(settings, ledger)
    result = await gateway.structured(
        GatewayCallContext(
            session.id, f"preflight-{uuid4()}", datetime.now(UTC) + timedelta(seconds=60)
        ),
        instructions="Return only the requested structured status.",
        input_text="Return status ok.",
        output_type=ConnectivityResult,
    )
    if result.status != "ok":
        raise RuntimeError("unexpected live result")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    json.loads((root / "data" / "approved_seed.json").read_text(encoding="utf-8"))
    json.loads((root / "data" / "ontology.json").read_text(encoding="utf-8"))
    settings = settings_from_environment(runtime_dir=root / "runtime")
    print("Fixture JSON: OK")
    print(f"Python target: 3.12; environment: {settings.app_env}")
    blockers = settings.live_blockers(now=datetime.now(UTC))
    print(f"Live LLM readiness: {'ready' if not blockers else 'blocked'}")
    if args.live:
        asyncio.run(live_check())
        print("Gated live API check: OK")
    else:
        print("No API request was made (use --live plus RUN_LIVE_TESTS=1 explicitly).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
