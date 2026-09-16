from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agents import MaxTurnsExceeded, ModelBehaviorError, ModelRefusalError
from agents.tool_context import ToolContext

from wg4_demo.agent_runtime import translate_agent_error
from wg4_demo.errors import SearchFailure
from wg4_demo.evidence import EvidenceService
from wg4_demo.graph import GraphService
from wg4_demo.repository import Repository
from wg4_demo.retrieval import RetrievalService
from wg4_demo.schemas import SessionRecord
from wg4_demo.tools import ToolRuntimeContext, propose_update, search_knowledge


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            ModelBehaviorError("Invalid JSON input for tool propose_update"),
            "agent_tool_input_invalid",
        ),
        (ModelBehaviorError("Invalid JSON when parsing model output"), "agent_output_invalid"),
        (ModelRefusalError("refused"), "agent_refusal"),
        (MaxTurnsExceeded("limit"), "agent_turn_limit"),
    ],
)
def test_agent_sdk_errors_become_safe_validation_codes(
    error: BaseException, expected_code: str
) -> None:
    translated = translate_agent_error(error)
    assert translated.code == expected_code
    assert translated.stage == "validation"


def test_update_tool_exposes_unambiguous_operation_variants() -> None:
    schema = json.dumps(propose_update.params_json_schema)
    assert "AddActionPrerequisiteToolOperation" in schema
    assert "AddFactToolOperation" in schema
    assert "ReplaceFactToolOperation" in schema
    assert "RemoveFactToolOperation" in schema


class FailingRetrieval(RetrievalService):
    def search_knowledge(self, *args, **kwargs):
        raise SearchFailure("injected search failure")


@pytest.mark.asyncio
async def test_function_tool_raises_instead_of_returning_model_error(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace = repository.create_workspace(participant.id, seed_mode="from_scratch")
    failing = FailingRetrieval(repository, project_root / "data" / "vocabulary.json")
    runtime = ToolRuntimeContext(
        session_id=participant.id,
        workspace_id=workspace.id,
        action_id="tool-failure",
        mode="qa",
        kb_revision=workspace.kb_revision,
        deadline=datetime.now(UTC) + timedelta(seconds=10),
        repository=repository,
        retrieval=failing,
        graph=GraphService(repository),
        evidence=EvidenceService(repository),
        max_tool_calls=8,
    )
    context = ToolContext(
        context=runtime,
        tool_name="search_knowledge",
        tool_call_id="call-1",
        tool_arguments='{"query":"冷却器1"}',
    )

    with pytest.raises(SearchFailure):
        await search_knowledge.on_invoke_tool(context, '{"query":"冷却器1"}')
    assert search_knowledge._failure_error_function is None
    assert search_knowledge.timeout_behavior == "raise_exception"
