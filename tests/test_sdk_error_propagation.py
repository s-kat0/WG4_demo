from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agents.tool_context import ToolContext

from wg4_demo.errors import SearchFailure
from wg4_demo.evidence import EvidenceService
from wg4_demo.graph import GraphService
from wg4_demo.repository import Repository
from wg4_demo.retrieval import RetrievalService
from wg4_demo.schemas import SessionRecord
from wg4_demo.tools import ToolRuntimeContext, search_knowledge


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
