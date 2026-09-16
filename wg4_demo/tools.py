"""Agents SDK function tools with failures propagated outside the runner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agents import RunContextWrapper, function_tool

from wg4_demo.errors import AppError, AuthorizationError, StaleContextError
from wg4_demo.evidence import EvidenceService
from wg4_demo.graph import GraphService
from wg4_demo.repository import Repository, canonical_json
from wg4_demo.result_validation import ToolTrace
from wg4_demo.retrieval import RetrievalService
from wg4_demo.schemas import CauseStatus, ProposalOperation


@dataclass(slots=True)
class ToolRuntimeContext:
    session_id: str
    workspace_id: str
    action_id: str
    mode: str
    kb_revision: int
    deadline: datetime
    repository: Repository
    retrieval: RetrievalService
    graph: GraphService
    evidence: EvidenceService
    max_tool_calls: int
    submitted_segment_ids: set[str] = field(default_factory=set)
    trace: ToolTrace = field(default_factory=ToolTrace)
    staged_proposal_id: str | None = None
    is_active: Callable[[], bool] = lambda: True

    def before_tool(self, name: str) -> None:
        if datetime.now(UTC) >= self.deadline:
            raise AppError("action_timeout", "実行期限を迎えました。", "tool")
        if not self.is_active():
            raise AppError("job_cancelled", "操作が取り消されました。", "tool")
        current = self.repository.require_workspace(self.session_id, self.workspace_id)
        if current.kb_revision != self.kb_revision:
            raise StaleContextError()
        if len(self.trace.calls) >= self.max_tool_calls:
            raise AppError("tool_call_limit", "この操作のツール呼出し上限です。", "tool")
        self.trace.calls.append(name)


@function_tool(
    failure_error_function=None,
    timeout=10,
    timeout_behavior="raise_exception",
)
async def search_knowledge(
    context: RunContextWrapper[ToolRuntimeContext],
    query: str,
    equipment_name: str | None = None,
) -> str:
    """Search only current approved knowledge in this operation's workspace."""
    runtime = context.context
    runtime.before_tool("search_knowledge")
    result = runtime.retrieval.search_knowledge(
        runtime.workspace_id, query, equipment_name=equipment_name
    )
    runtime.trace.search_result = result
    return result.model_dump_json()


@function_tool(
    failure_error_function=None,
    timeout=10,
    timeout_behavior="raise_exception",
)
async def get_context(
    context: RunContextWrapper[ToolRuntimeContext], knowledge_id: str, version: int
) -> str:
    """Traverse the approved knowledge graph for a previously searched active version."""
    runtime = context.context
    runtime.before_tool("get_context")
    searched = runtime.trace.search_result
    if searched is None or not any(
        hit.knowledge_id == knowledge_id and hit.version == version for hit in searched.hits
    ):
        raise AuthorizationError("検索で取得していない知識は探索できません。")
    result = runtime.graph.get_context(runtime.workspace_id, knowledge_id, version)
    runtime.trace.context_versions.add((knowledge_id, version))
    return canonical_json(
        {
            "knowledge_id": result.knowledge_id,
            "version": result.version,
            "nodes": result.nodes,
            "edges": result.edges,
        }
    )


@function_tool(
    failure_error_function=None,
    timeout=10,
    timeout_behavior="raise_exception",
)
async def read_evidence(
    context: RunContextWrapper[ToolRuntimeContext], segment_ids: list[str]
) -> str:
    """Read at most six permitted source segments; never broadens the evidence scope."""
    runtime = context.context
    runtime.before_tool("read_evidence")
    if runtime.mode == "update":
        rows = runtime.evidence.read_for_update(
            runtime.workspace_id,
            segment_ids,
            submitted_segment_ids=runtime.submitted_segment_ids,
        )
    else:
        rows = runtime.evidence.read_for_qa(runtime.workspace_id, segment_ids)
    runtime.trace.evidence_ids.update(segment_ids)
    return canonical_json(rows)


@function_tool(
    failure_error_function=None,
    timeout=10,
    timeout_behavior="raise_exception",
)
async def propose_update(
    context: RunContextWrapper[ToolRuntimeContext],
    target_id: str,
    base_version: int,
    operations: list[ProposalOperation],
    reason: str,
) -> str:
    """Stage an update proposal; this does not approve or publish it."""
    runtime = context.context
    runtime.before_tool("propose_update")
    if runtime.mode != "update":
        raise AuthorizationError("更新モード以外では更新案を作成できません。")
    if runtime.staged_proposal_id is not None:
        raise AppError("duplicate_proposal", "同じ操作で複数の更新案は作成できません。", "tool")
    if (target_id, base_version) not in runtime.trace.context_versions:
        raise AuthorizationError("探索していない知識版へ更新案を作成できません。")
    item = runtime.repository.get_knowledge(runtime.workspace_id, target_id, base_version)
    allowed_segments = runtime.trace.evidence_ids | runtime.submitted_segment_ids
    proposal = runtime.repository.stage_proposal(
        runtime.workspace_id,
        action_id=runtime.action_id,
        target_item_id=target_id,
        base_version=base_version,
        operations=operations,
        reason=reason,
        equipment=item.equipment,
        case_label=item.case_label,
        missing_fields=item.missing_fields,
        cause_status=CauseStatus(item.cause_status),
        allowed_segment_ids=allowed_segments,
    )
    runtime.staged_proposal_id = proposal.id
    return canonical_json(
        {
            "proposal_id": proposal.id,
            "status": "staged",
            "content_hash": proposal.content_hash,
        }
    )


ALL_TOOLS = [search_knowledge, get_context, read_evidence, propose_update]
QA_TOOLS = [search_knowledge, get_context, read_evidence]
