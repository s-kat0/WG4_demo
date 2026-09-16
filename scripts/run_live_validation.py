"""Explicit, finite-budget live validation of the real application stack."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

os.environ["OPENAI_AGENTS_DONT_LOG_MODEL_DATA"] = "1"
os.environ["OPENAI_AGENTS_DONT_LOG_TOOL_DATA"] = "1"

from argon2 import PasswordHasher
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wg4_demo.errors import AppError
from wg4_demo.jobs import JobRecord
from wg4_demo.schemas import (
    TERMINAL_JOB_STATES,
    CauseStatus,
    FactKind,
    JobState,
    KnowledgeDraft,
    OperationType,
    ProposalOperation,
    Role,
)
from wg4_demo.services import Services, build_services
from wg4_demo.settings import Settings, settings_from_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-5.6-luna"
JobHandler = Callable[[JobRecord], tuple[str, dict[str, object]]]


def diagnostic_handler(handler: JobHandler) -> JobHandler:
    def wrapped(job: JobRecord) -> tuple[str, dict[str, object]]:
        try:
            return handler(job)
        except AppError:
            raise
        except BaseException as exc:
            raise AppError(
                f"live_unhandled_{type(exc).__name__.lower()}",
                "実API検証で未処理例外を検出しました。",
                "live_validation",
            ) from exc

    return wrapped


def live_settings(
    runtime_dir: Path,
    *,
    reasoning_effort: Literal["none", "low"],
    call_budget: int,
) -> Settings:
    base = settings_from_environment(runtime_dir=runtime_dir)
    if base.openai_api_key is None:
        raise RuntimeError("OPENAI_API_KEY is required")
    hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
    return base.model_copy(
        update={
            "app_env": "local",
            "openai_model": MODEL,
            "openai_reasoning_effort": reasoning_effort,
            "demo_password_hash": hasher.hash(secrets.token_urlsafe(32)),
            "admin_password_hash": hasher.hash(secrets.token_urlsafe(32)),
            "auth_version": f"live-validation-{uuid4()}",
            "demo_expires_at": datetime.now(UTC) + timedelta(hours=1),
            "app_llm_enabled": True,
            "app_max_llm_calls": call_budget,
            "session_max_llm_calls": call_budget,
            "max_model_calls_per_action": 6,
            "max_concurrent_jobs": 1,
            "max_concurrent_llm": 1,
            "global_rpm": 20,
            "global_tpm": 200_000,
            "request_timeout_seconds": 60,
            "action_timeout_seconds": 180,
            "queue_wait_timeout_seconds": 30,
            "runtime_dir": runtime_dir,
        }
    )


def build_validation_services(settings: Settings) -> tuple[Services, str]:
    participant_password = secrets.token_urlsafe(32)
    admin_password = secrets.token_urlsafe(32)
    hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
    configured = settings.model_copy(
        update={
            "demo_password_hash": SecretStr(hasher.hash(participant_password)),
            "admin_password_hash": SecretStr(hasher.hash(admin_password)),
        }
    )
    services = build_services(configured, project_root=PROJECT_ROOT)
    services.scheduler.handlers = {
        mode: diagnostic_handler(handler) for mode, handler in services.scheduler.handlers.items()
    }
    participant = services.auth.login(
        participant_password,
        role=Role.PARTICIPANT,
        client_token=f"live-participant-{uuid4()}",
    )
    admin = services.auth.login(
        admin_password,
        role=Role.ADMIN,
        client_token=f"live-admin-{uuid4()}",
    )
    services.ledger.enable_budget(
        admin.id,
        additional_calls=configured.app_max_llm_calls,
        confirmed_external_limit=True,
        reason="explicit live validation after user confirmed project hard spend limit",
    )
    return services, participant.id


def run_job(
    services: Services,
    *,
    session_id: str,
    workspace_id: str,
    conversation_id: str,
    mode: str,
    payload: dict[str, Any],
    phase: str | None = None,
) -> tuple[dict[str, Any], float]:
    phase_name = phase or mode
    workspace = services.repository.require_workspace(session_id, workspace_id)
    started = time.monotonic()
    job = services.jobs.enqueue(
        session_id=session_id,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        mode=mode,
        payload=payload,
        kb_revision=workspace.kb_revision,
        model_id=services.settings.openai_model or "UNCONFIGURED",
        model_settings={
            "store": False,
            "parallel_tool_calls": False,
            "max_output_tokens": services.settings.max_output_tokens,
            "reasoning_effort": services.settings.openai_reasoning_effort,
        },
        prompt_version="wg4-prompts-v13",
        schema_version="wg4-schema-v2",
        dedupe_key=str(uuid4()),
    )
    services.scheduler.wake()
    monitor_deadline = time.monotonic() + services.settings.action_timeout_seconds + 10
    while time.monotonic() < monitor_deadline:
        current = services.jobs.get(session_id=session_id, job_id=job.job_id)
        if current.state in TERMINAL_JOB_STATES:
            elapsed = time.monotonic() - started
            if current.state is not JobState.SUCCEEDED:
                raise RuntimeError(
                    f"{phase_name} ended as {current.state.value}: "
                    f"{current.safe_error_code or 'unknown'}"
                )
            outcome = services.repository.get_action_outcome(
                workspace_id, current.action_id, session_id=session_id
            )
            if outcome is None:
                raise RuntimeError(f"{phase_name} outcome missing")
            return dict(outcome["payload"]), elapsed
        time.sleep(0.1)
    services.jobs.cancel(session_id=session_id, job_id=job.job_id)
    raise RuntimeError(f"{phase_name} monitor timeout; cancellation requested")


def register_fixture_sources(
    services: Services, workspace_id: str
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, Any]]:
    maintenance = json.loads((PROJECT_ROOT / "data/maintenance1.json").read_text("utf-8"))
    interview = json.loads((PROJECT_ROOT / "data/interview1.json").read_text("utf-8"))
    demo = json.loads((PROJECT_ROOT / "data/demo_inputs.json").read_text("utf-8"))
    _, mapping = services.repository.register_source(
        workspace_id,
        title=maintenance["title"],
        kind=maintenance["kind"],
        equipment=maintenance["equipment"],
        case_label=maintenance["case_label"],
        external_key=f"live-maintenance-{uuid4()}",
        segments=[
            (segment["key"], "document", segment["text"]) for segment in maintenance["segments"]
        ],
    )
    segments = [
        {
            "segment_id": mapping[segment["key"]],
            "text": segment["text"],
            "speaker": "document",
        }
        for segment in maintenance["segments"]
    ]
    return segments, interview, demo


def validate_draft(draft: dict[str, Any], segments: list[dict[str, str]]) -> None:
    source = {segment["segment_id"]: segment["text"] for segment in segments}
    if draft["cause_status"] != CauseStatus.UNRESOLVED.value:
        raise RuntimeError("extraction incorrectly resolved the cause")
    required_kinds = {
        FactKind.OBSERVATION.value,
        FactKind.CONDITION.value,
        FactKind.CHECK_ACTION.value,
    }
    actual_kinds = {fact["kind"] for fact in draft["facts"]}
    if not required_kinds.issubset(actual_kinds):
        raise RuntimeError("extraction omitted a required fact kind")
    for fact in draft["facts"]:
        if fact["kind"] == FactKind.CAUSE_HYPOTHESIS.value:
            raise RuntimeError("extraction invented a cause hypothesis")
        if len(fact["text"]) > 30:
            raise RuntimeError("extraction returned a fact longer than 30 characters")
        for evidence in fact["evidence"]:
            if evidence["segment_id"] not in source:
                raise RuntimeError("extraction returned an unknown evidence id")
            if evidence["quote"] not in source[evidence["segment_id"]]:
                raise RuntimeError("extraction returned a non-verbatim quote")


def usage_summary(control_db: Path) -> dict[str, int]:
    connection = sqlite3.connect(control_db)
    try:
        row = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
            FROM api_calls
            """
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return {"calls": int(row[0]), "input_tokens": int(row[1]), "output_tokens": int(row[2])}


def extract_only(services: Services, session_id: str) -> dict[str, Any]:
    workspace = services.repository.create_workspace(
        session_id,
        seed_mode="from_scratch",
    )
    conversation = services.repository.create_conversation(workspace.id)
    segments, _, _ = register_fixture_sources(services, workspace.id)
    extraction, extract_seconds = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=conversation,
        mode="extract",
        payload={"segments": segments},
    )
    validate_draft(extraction["draft"], segments)
    return {
        "extract_seconds": round(extract_seconds, 3),
        "facts": [
            {"kind": fact["kind"], "text": fact["text"]} for fact in extraction["draft"]["facts"]
        ],
        "cause_status": extraction["draft"]["cause_status"],
        "usage": usage_summary(services.settings.control_db_path),
    }


def smoke(services: Services, session_id: str) -> dict[str, Any]:
    workspace = services.repository.create_workspace(
        session_id,
        seed_mode="approved_v1",
        seed_path=PROJECT_ROOT / "data/approved_seed.json",
    )
    conversation = services.repository.create_conversation(workspace.id)
    segments, _, demo = register_fixture_sources(services, workspace.id)
    extraction, extract_seconds = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=conversation,
        mode="extract",
        payload={"segments": segments},
    )
    validate_draft(extraction["draft"], segments)
    services.repository.append_message(
        workspace.id, conversation, role="user", text=demo["main_question"]
    )
    answer, qa_seconds = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=conversation,
        mode="qa",
        payload={"question": demo["main_question"]},
    )
    selection = answer["selection"]
    if selection["status"] != "candidates" or not selection["candidates"]:
        raise RuntimeError("qa did not return a grounded candidate")
    required_tools = {"search_knowledge", "get_context", "read_evidence"}
    if not required_tools.issubset(set(answer["tools"])):
        raise RuntimeError("qa did not execute all required retrieval tools")
    return {
        "extract_seconds": round(extract_seconds, 3),
        "qa_seconds": round(qa_seconds, 3),
        "qa_status": selection["status"],
        "qa_tools": answer["tools"],
        "usage": usage_summary(services.settings.control_db_path),
    }


def full(services: Services, session_id: str) -> dict[str, Any]:
    workspace = services.repository.create_workspace(
        session_id,
        seed_mode="from_scratch",
    )
    conversation = services.repository.create_conversation(workspace.id)
    segments, interview, demo = register_fixture_sources(services, workspace.id)
    timings: dict[str, float] = {}
    extraction, timings["extract"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=conversation,
        mode="extract",
        payload={"segments": segments},
    )
    validate_draft(extraction["draft"], segments)

    _, opening_mapping = services.repository.register_source(
        workspace.id,
        title="聞き取り記録1（開始発言）",
        kind="interview",
        equipment="冷却器1",
        case_label="事例1",
        external_key=f"live-opening-{uuid4()}",
        segments=[(f"live-opening-{uuid4()}", "operator", interview["opening"])],
    )
    opening = {
        "segment_id": next(iter(opening_mapping.values())),
        "text": interview["opening"],
        "speaker": "operator",
    }
    question, timings["interview"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=conversation,
        mode="interview",
        payload={"draft": extraction["draft"], "statements": [opening]},
    )
    if not any(term in question["question"] for term in ("流量", "入口温度")):
        raise RuntimeError("interview did not ask the prioritized missing condition")

    _, answer_mapping = services.repository.register_source(
        workspace.id,
        title="聞き取り記録1（質問と回答）",
        kind="interview",
        equipment="冷却器1",
        case_label="事例1",
        external_key=f"live-answer-{uuid4()}",
        segments=[
            (f"live-question-{uuid4()}", "assistant", question["question"]),
            (f"live-answer-{uuid4()}", "operator", interview["prepared_answer"]),
        ],
    )
    answer_ids = list(answer_mapping.values())
    new_segments = [
        {"segment_id": answer_ids[0], "text": question["question"], "speaker": "assistant"},
        {
            "segment_id": answer_ids[1],
            "text": interview["prepared_answer"],
            "speaker": "operator",
        },
    ]
    all_segments = [*segments, opening, *new_segments]
    allowed_ids = [
        segment["segment_id"] for segment in all_segments if segment["speaker"] != "assistant"
    ]
    reflected, timings["reflect"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=conversation,
        mode="reflect",
        payload={
            "segments": all_segments,
            "allowed_segment_ids": allowed_ids,
            "equipment": "冷却器1",
            "case_label": "事例1",
        },
    )
    proposal = services.repository.get_proposal(workspace.id, reflected["proposal_id"])
    if proposal.status != "pending":
        raise RuntimeError("reflection proposal is not pending")
    approval = services.approvals.approve(
        session_id=session_id,
        workspace_id=workspace.id,
        proposal_id=proposal.id,
        expected_content_hash=proposal.content_hash,
    )
    if approval.after_version != 1:
        raise RuntimeError("first human approval did not create v1")

    services.repository.append_message(
        workspace.id, conversation, role="user", text=demo["main_question"]
    )
    first_answer, timings["qa_v1"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=conversation,
        mode="qa",
        phase="qa_v1",
        payload={"question": demo["main_question"]},
    )
    required_tools = {"search_knowledge", "get_context", "read_evidence"}
    if not required_tools.issubset(set(first_answer["tools"])):
        raise RuntimeError("v1 qa did not execute all required retrieval tools")

    item3 = services.repository.get_knowledge(workspace.id, approval.item_id)
    _, update_mapping = services.repository.register_source(
        workspace.id,
        title="聞き取り記録2",
        kind="interview",
        equipment=item3.equipment,
        case_label=item3.case_label,
        external_key=f"live-update-{uuid4()}",
        segments=[(f"live-update-p1-{uuid4()}", "operator", demo["update_statement"])],
    )
    update_segment_id = next(iter(update_mapping.values()))
    update, timings["update"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=conversation,
        mode="update",
        payload={
            "statement": demo["update_statement"],
            "submitted_segment_ids": [update_segment_id],
            "target_knowledge_id": item3.id,
            "target_version": item3.version,
            "equipment": item3.equipment,
        },
    )
    pending = services.repository.get_proposal(workspace.id, update["proposal_id"])
    if services.repository.get_knowledge(workspace.id, item3.id).version != 1:
        raise RuntimeError("pending update changed approved knowledge")
    update_approval = services.approvals.approve(
        session_id=session_id,
        workspace_id=workspace.id,
        proposal_id=pending.id,
        expected_content_hash=pending.content_hash,
    )
    if update_approval.after_version != 2:
        raise RuntimeError("update approval did not create v2")

    new_conversation = services.repository.create_conversation(workspace.id)
    if services.repository.list_messages(workspace.id, new_conversation):
        raise RuntimeError("new conversation inherited old messages")
    services.repository.append_message(
        workspace.id, new_conversation, role="user", text=demo["main_question"]
    )
    second_answer, timings["qa_v2"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=new_conversation,
        mode="qa",
        phase="qa_v2",
        payload={"question": demo["main_question"]},
    )
    candidates = second_answer["selection"]["candidates"]
    if not any(
        candidate["knowledge_id"] == item3.id
        and candidate["version"] == 2
        and update_segment_id in candidate["evidence_segment_ids"]
        for candidate in candidates
    ):
        raise RuntimeError("new conversation did not retrieve v2 with the added evidence")
    return {
        "timings_seconds": {key: round(value, 3) for key, value in timings.items()},
        "v1_tools": first_answer["tools"],
        "update_tools": update["tools"],
        "v2_tools": second_answer["tools"],
        "final_version": 2,
        "added_evidence_retrieved": True,
        "usage": usage_summary(services.settings.control_db_path),
    }


def _v5_draft_payload(item: Any) -> dict[str, Any]:
    return {
        "facts": [
            {
                "kind": fact.kind.value,
                "text": fact.text,
                "condition_scope": fact.condition_scope.value if fact.condition_scope else None,
                "parent_action_fact_id": fact.parent_action_fact_id,
                "evidence": [
                    {"segment_id": ref.segment_id, "quote": ref.quote} for ref in fact.evidence_refs
                ],
            }
            for fact in item.facts
        ],
        "cause_status": item.cause_status.value,
        "missing_fields": item.missing_fields,
    }


def _v5_comparison(
    services: Services,
    *,
    session_id: str,
    workspace_id: str,
    item: Any,
    question: str,
    stage: str,
) -> tuple[dict[str, Any], float]:
    conversation = services.repository.create_conversation(workspace_id)
    state = services.conversations.prepare_turn(
        workspace_id, conversation, question, selected_knowledge_id=item.id
    )
    services.repository.append_message(workspace_id, conversation, role="user", text=question)
    return run_job(
        services,
        session_id=session_id,
        workspace_id=workspace_id,
        conversation_id=conversation,
        mode="qa",
        phase=f"comparison_{stage}",
        payload={
            "question": question,
            "consultation": state.model_dump(mode="json"),
            "comparison_stage": stage,
            "empty_history": True,
            "target_item_id": item.id,
            "target_version": item.version,
        },
    )


def full_v5(services: Services, session_id: str) -> dict[str, Any]:
    """Run the v5 A/B/C scenario only behind the explicit live gate."""

    demo = json.loads((PROJECT_ROOT / "data/demo_inputs_v5.json").read_text("utf-8"))
    workspace = services.repository.create_workspace(
        session_id,
        seed_mode="practical_v5",
        seed_path=PROJECT_ROOT / "data/knowledge_seed_v5.json",
    )
    setup_conversation = services.repository.create_conversation(workspace.id)
    timings: dict[str, float] = {}
    _, document_mapping = services.repository.register_source(
        workspace.id,
        title=demo["document"]["title"],
        kind="document",
        equipment=demo["document"]["equipment"],
        case_label=demo["document"]["case"],
        external_key=f"live-v5-document-{uuid4()}",
        segments=[(f"live-v5-document-p1-{uuid4()}", "document", demo["document"]["text"])],
    )
    document_segment_id = next(iter(document_mapping.values()))
    document_segments = [
        {
            "segment_id": document_segment_id,
            "text": demo["document"]["text"],
            "speaker": "document",
        }
    ]
    extraction, timings["extract"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=setup_conversation,
        mode="extract",
        payload={"segments": document_segments},
    )
    validate_draft(extraction["draft"], document_segments)
    draft = KnowledgeDraft.model_validate(extraction["draft"])
    document_proposal = services.repository.stage_proposal(
        workspace.id,
        action_id=f"live-v5-document-proposal-{uuid4()}",
        target_item_id=None,
        base_version=0,
        operations=[
            ProposalOperation(operation=OperationType.ADD_FACT, new_fact=fact)
            for fact in draft.facts
        ],
        reason="v5 live document-only version",
        equipment=demo["document"]["equipment"],
        case_label=demo["document"]["case"],
        title="温度計交換後の出口温度表示",
        missing_fields=draft.missing_fields,
        cause_status=draft.cause_status,
        allowed_segment_ids={document_segment_id},
    )
    services.repository.publish_proposal(workspace.id, document_proposal.id)
    document_approval = services.approvals.approve(
        session_id=session_id,
        workspace_id=workspace.id,
        proposal_id=document_proposal.id,
        expected_content_hash=document_proposal.content_hash,
    )
    item = services.repository.get_knowledge(workspace.id, document_approval.item_id)
    if item.version != 1 or len(services.repository.list_knowledge(workspace.id)) != 13:
        raise RuntimeError("v5 document approval did not create one thirteenth item")

    answer_a, timings["qa_a"] = _v5_comparison(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        item=item,
        question=demo["comparison_question"],
        stage="A",
    )

    _, opening_mapping = services.repository.register_source(
        workspace.id,
        title=demo["interview"]["title"],
        kind="interview",
        equipment=item.equipment,
        case_label=item.case_label,
        external_key=f"live-v5-opening-{uuid4()}",
        segments=[(f"live-v5-opening-p1-{uuid4()}", "operator", demo["interview"]["opening"])],
    )
    opening = {
        "segment_id": next(iter(opening_mapping.values())),
        "text": demo["interview"]["opening"],
        "speaker": "operator",
    }
    statements = [opening]
    first_question, timings["interview_reason_question"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=setup_conversation,
        mode="interview",
        payload={"draft": _v5_draft_payload(item), "statements": statements},
    )
    if not any(term in first_question["question"] for term in ("理由", "なぜ", "考え")):
        raise RuntimeError("v5 interview did not advance to the decision reason")
    _, reason_mapping = services.repository.register_source(
        workspace.id,
        title="聞き取り記録1（理由）",
        kind="interview",
        equipment=item.equipment,
        case_label=item.case_label,
        external_key=f"live-v5-reason-{uuid4()}",
        segments=[
            (f"live-v5-reason-q-{uuid4()}", "assistant", first_question["question"]),
            (f"live-v5-reason-a-{uuid4()}", "operator", demo["interview"]["reason_reply"]),
        ],
    )
    reason_ids = list(reason_mapping.values())
    statements.extend(
        [
            {
                "segment_id": reason_ids[0],
                "text": first_question["question"],
                "speaker": "assistant",
            },
            {
                "segment_id": reason_ids[1],
                "text": demo["interview"]["reason_reply"],
                "speaker": "operator",
            },
        ]
    )
    second_question, timings["interview_scope_question"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=setup_conversation,
        mode="interview",
        payload={"draft": _v5_draft_payload(item), "statements": statements},
    )
    _, scope_mapping = services.repository.register_source(
        workspace.id,
        title="聞き取り記録1（適用範囲）",
        kind="interview",
        equipment=item.equipment,
        case_label=item.case_label,
        external_key=f"live-v5-scope-{uuid4()}",
        segments=[
            (f"live-v5-scope-q-{uuid4()}", "assistant", second_question["question"]),
            (f"live-v5-scope-a-{uuid4()}", "operator", demo["interview"]["scope_reply"]),
        ],
    )
    scope_ids = list(scope_mapping.values())
    statements.extend(
        [
            {
                "segment_id": scope_ids[0],
                "text": second_question["question"],
                "speaker": "assistant",
            },
            {
                "segment_id": scope_ids[1],
                "text": demo["interview"]["scope_reply"],
                "speaker": "operator",
            },
        ]
    )
    allowed_ids = [
        statement["segment_id"] for statement in statements if statement["speaker"] == "operator"
    ]
    supplement, timings["supplement"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=setup_conversation,
        mode="supplement",
        payload={
            "target_item_id": item.id,
            "target_version": item.version,
            "statements": statements,
            "allowed_segment_ids": allowed_ids,
        },
    )
    supplement_proposal = services.repository.get_proposal(workspace.id, supplement["proposal_id"])
    supplement_approval = services.approvals.approve(
        session_id=session_id,
        workspace_id=workspace.id,
        proposal_id=supplement_proposal.id,
        expected_content_hash=supplement_proposal.content_hash,
    )
    if supplement_approval.after_version != 2:
        raise RuntimeError("v5 interview supplement did not create v2")
    item = services.repository.get_knowledge(workspace.id, item.id)
    answer_b, timings["qa_b"] = _v5_comparison(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        item=item,
        question=demo["comparison_question"],
        stage="B",
    )
    b_candidate = next(
        (
            candidate
            for candidate in answer_b["selection"]["candidates"]
            if candidate["knowledge_id"] == item.id and candidate["version"] == 2
        ),
        None,
    )
    if b_candidate is None or not (
        {reason_ids[1], scope_ids[1]} & set(b_candidate["evidence_segment_ids"])
    ):
        raise RuntimeError("answer B did not retrieve the approved interview evidence")

    _, feedback_mapping = services.repository.register_source(
        workspace.id,
        title=demo["feedback"]["title"],
        kind="interview",
        equipment=item.equipment,
        case_label=item.case_label,
        external_key=f"live-v5-feedback-{uuid4()}",
        segments=[(f"live-v5-feedback-p1-{uuid4()}", "operator", demo["feedback"]["text"])],
    )
    feedback_segment_id = next(iter(feedback_mapping.values()))
    feedback, timings["feedback"] = run_job(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        conversation_id=setup_conversation,
        mode="update",
        payload={
            "statement": demo["feedback"]["text"],
            "submitted_segment_ids": [feedback_segment_id],
            "target_knowledge_id": item.id,
            "target_version": item.version,
            "equipment": item.equipment,
            "answer_action_id": next(
                snapshot.action_id
                for snapshot in services.repository.list_answer_snapshots(workspace.id)
                if snapshot.stage == "B"
            ),
        },
    )
    feedback_proposal = services.repository.get_proposal(workspace.id, feedback["proposal_id"])
    if services.repository.get_knowledge(workspace.id, item.id).version != 2:
        raise RuntimeError("pending feedback changed approved knowledge")
    feedback_approval = services.approvals.approve(
        session_id=session_id,
        workspace_id=workspace.id,
        proposal_id=feedback_proposal.id,
        expected_content_hash=feedback_proposal.content_hash,
    )
    if feedback_approval.after_version != 3:
        raise RuntimeError("v5 feedback did not create v3")
    item = services.repository.get_knowledge(workspace.id, item.id)
    answer_c, timings["qa_c"] = _v5_comparison(
        services,
        session_id=session_id,
        workspace_id=workspace.id,
        item=item,
        question=demo["comparison_question"],
        stage="C",
    )
    if not any(
        candidate["knowledge_id"] == item.id
        and candidate["version"] == 3
        and feedback_segment_id in candidate["evidence_segment_ids"]
        for candidate in answer_c["selection"]["candidates"]
    ):
        raise RuntimeError("answer C did not retrieve v3 with feedback evidence")
    snapshots = services.repository.list_answer_snapshots(workspace.id)
    if [snapshot.stage for snapshot in snapshots] != ["A", "B", "C"]:
        raise RuntimeError("v5 comparison snapshots are incomplete")
    return {
        "timings_seconds": {key: round(value, 3) for key, value in timings.items()},
        "answer_tools": {
            "A": answer_a["tools"],
            "B": answer_b["tools"],
            "C": answer_c["tools"],
        },
        "final_version": item.version,
        "knowledge_count": len(services.repository.list_knowledge(workspace.id)),
        "comparison_stages": [snapshot.stage for snapshot in snapshots],
        "usage": usage_summary(services.settings.control_db_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["extract", "smoke", "full"], required=True)
    parser.add_argument("--reasoning-effort", choices=["none", "low"], required=True)
    parser.add_argument("--call-budget", type=int, required=True)
    parser.add_argument("--confirmed-external-limit", action="store_true")
    args = parser.parse_args()
    if os.environ.get("RUN_LIVE_TESTS") != "1":
        raise SystemExit("RUN_LIVE_TESTS=1 is required")
    if not args.confirmed_external_limit:
        raise SystemExit("--confirmed-external-limit is required")
    if not 1 <= args.call_budget <= 40:
        raise SystemExit("--call-budget must be between 1 and 40")

    with tempfile.TemporaryDirectory(prefix="wg4-live-", dir="/private/tmp") as directory:
        settings = live_settings(
            Path(directory),
            reasoning_effort=args.reasoning_effort,
            call_budget=args.call_budget,
        )
        services, participant_id = build_validation_services(settings)
        started = time.monotonic()
        try:
            if args.mode == "extract":
                result = extract_only(services, participant_id)
            elif args.mode == "smoke":
                result = smoke(services, participant_id)
            else:
                result = full_v5(services, participant_id)
        except AppError as exc:
            print(json.dumps({"status": "failed", "code": exc.code, "stage": exc.stage}))
            return 1
        except RuntimeError as exc:
            print(json.dumps({"status": "failed", "code": "validation_failed", "detail": str(exc)}))
            return 1
        except Exception as exc:
            print(
                json.dumps(
                    {"status": "failed", "code": "validation_failed", "type": type(exc).__name__}
                )
            )
            return 1
        finally:
            services.scheduler.stop()
        print(
            json.dumps(
                {
                    "status": "passed",
                    "mode": args.mode,
                    "model": MODEL,
                    "reasoning_effort": args.reasoning_effort,
                    "total_seconds": round(time.monotonic() - started, 3),
                    **result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
