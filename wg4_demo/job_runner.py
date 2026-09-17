"""Translate frozen job payloads into domain/agent operations."""

from __future__ import annotations

import asyncio
from typing import Any

from wg4_demo.agent_runtime import AgentService, StructuredWorkflowService
from wg4_demo.jobs import JobRecord, JobService
from wg4_demo.llm_gateway import GatewayCallContext


class ApplicationJobRunner:
    def __init__(
        self,
        jobs: JobService,
        structured: StructuredWorkflowService,
        agents: AgentService,
    ) -> None:
        self.jobs = jobs
        self.structured = structured
        self.agents = agents

    def __call__(self, job: JobRecord) -> tuple[str, dict[str, object]]:
        return asyncio.run(self._dispatch(job))

    async def _dispatch(self, job: JobRecord) -> tuple[str, dict[str, object]]:
        if job.run_deadline_at is None:
            raise RuntimeError("running job has no deadline")
        call_context = GatewayCallContext(job.session_id, job.action_id, job.run_deadline_at)
        if job.mode == "extract":
            draft = await self.structured.extract(
                call_context, segments=self._string_dicts(job.payload["segments"])
            )
            return "knowledge_draft", {"draft": draft.model_dump(mode="json")}
        if job.mode == "interview":
            question = await self.structured.interview(
                call_context,
                draft=self._object_dict(job.payload["draft"]),
                statements=self._string_dicts(job.payload["statements"]),
            )
            return "interview_question", question.model_dump(mode="json")
        if job.mode == "reflect":
            proposal = await self.structured.reflect_and_stage(
                call_context,
                workspace_id=job.workspace_id,
                segments=self._string_dicts(job.payload["segments"]),
                allowed_segment_ids=set(self._string_list(job.payload["allowed_segment_ids"])),
                equipment=str(job.payload["equipment"]),
                case_label=(
                    str(job.payload["case_label"])
                    if job.payload.get("case_label") is not None
                    else None
                ),
            )
            return "proposal", {
                "proposal_id": proposal.id,
                "content_hash": proposal.content_hash,
            }
        if job.mode == "supplement":
            proposal = await self.structured.supplement_and_stage(
                call_context,
                workspace_id=job.workspace_id,
                target_item_id=str(job.payload["target_item_id"]),
                target_version=int(job.payload["target_version"]),
                statements=self._string_dicts(job.payload["statements"]),
                allowed_segment_ids=set(self._string_list(job.payload["allowed_segment_ids"])),
            )
            return "proposal", {
                "proposal_id": proposal.id,
                "content_hash": proposal.content_hash,
            }
        if job.mode == "qa":
            consultation = self._object_dict(job.payload.get("consultation", {}))
            intent = str(consultation.get("last_intent", "candidate_search"))
            focus_ids = set(self._string_list(consultation.get("focus_knowledge_ids", [])))
            explicit_selected = consultation.get("explicit_selected_knowledge_id")
            explicit_focus = isinstance(explicit_selected, str) and explicit_selected in focus_ids
            tool_context = self.agents.new_tool_context(
                session_id=job.session_id,
                workspace_id=job.workspace_id,
                action_id=job.action_id,
                mode="qa",
                kb_revision=job.kb_revision,
                deadline=job.run_deadline_at,
                answer_intent=intent,
                focus_knowledge_ids=focus_ids,
                explicit_focus=explicit_focus,
                is_active=lambda: self.jobs.heartbeat(job),
            )
            answer, completed_context = await self.agents.answer(
                call_context,
                tool_context,
                str(job.payload["question"]),
                conversation_context=consultation,
            )
            payload: dict[str, object] = {
                "selection": answer.model_dump(mode="json"),
                "tools": completed_context.trace.calls,
                "conversation_id": job.conversation_id,
            }
            comparison_stage = job.payload.get("comparison_stage")
            if comparison_stage is not None:
                payload["comparison"] = {
                    "stage": str(comparison_stage),
                    "question": str(job.payload["question"]),
                    "conversation_id": job.conversation_id,
                    "empty_history": job.payload.get("empty_history") is True,
                    "kb_revision": job.kb_revision,
                    "target_item_id": job.payload.get("target_item_id"),
                    "target_version": job.payload.get("target_version"),
                    "model_id": job.model_id,
                    "model_settings": job.model_settings,
                    "prompt_version": job.prompt_version,
                    "schema_version": job.schema_version,
                    "retrieval_version": "wg4-lexical-v2",
                }
            return "answer", payload
        if job.mode == "update":
            submitted_ids = set(self._string_list(job.payload["submitted_segment_ids"]))
            target_knowledge_id = str(job.payload["target_knowledge_id"])
            tool_context = self.agents.new_tool_context(
                session_id=job.session_id,
                workspace_id=job.workspace_id,
                action_id=job.action_id,
                mode="update",
                kb_revision=job.kb_revision,
                deadline=job.run_deadline_at,
                submitted_segment_ids=submitted_ids,
                focus_knowledge_ids={target_knowledge_id},
                is_active=lambda: self.jobs.heartbeat(job),
            )
            model_input = {
                "statement": str(job.payload["statement"]),
                "submitted_segment_ids": sorted(submitted_ids),
                "target_knowledge_id": target_knowledge_id,
                "target_version": int(job.payload["target_version"]),
                "equipment": str(job.payload["equipment"]),
            }
            proposal, completed_context = await self.agents.propose_update(
                call_context,
                tool_context,
                __import__("json").dumps(model_input, ensure_ascii=False),
            )
            return "proposal", {
                "proposal_id": proposal.id,
                "content_hash": proposal.content_hash,
                "tools": completed_context.trace.calls,
            }
        raise ValueError(f"unsupported job mode: {job.mode}")

    def _string_dicts(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise TypeError("expected list")
        output: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict) or not all(
                isinstance(key, str) and isinstance(entry, str) for key, entry in item.items()
            ):
                raise TypeError("expected list of string dictionaries")
            output.append(dict(item))
        return output

    def _object_dict(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("expected dictionary")
        return dict(value)

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError("expected string list")
        return value
