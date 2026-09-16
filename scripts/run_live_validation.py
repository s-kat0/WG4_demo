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
from wg4_demo.schemas import TERMINAL_JOB_STATES, CauseStatus, FactKind, JobState, Role
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
        prompt_version="wg4-prompts-v12",
        schema_version="wg4-schema-v1",
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
        seed_path=PROJECT_ROOT / "data/approved_seed.json",
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
        seed_path=PROJECT_ROOT / "data/approved_seed.json",
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
                result = full(services, participant_id)
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
