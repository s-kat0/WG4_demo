from __future__ import annotations

import os
import secrets
from typing import Any

import streamlit as st

from wg4_demo.jobs import JobRecord
from wg4_demo.safe_display import safe_error
from wg4_demo.schemas import JobState
from wg4_demo.services import Services


def new_dedupe_key() -> str:
    return secrets.token_urlsafe(18)


def active_job_exists(services: Services) -> bool:
    job_id = st.session_state.get("active_job_id")
    if not job_id:
        return False
    try:
        job = services.jobs.get(session_id=st.session_state.session_id, job_id=job_id)
    except Exception:
        return False
    return job.state in {JobState.QUEUED, JobState.RUNNING, JobState.CANCEL_REQUESTED}


def enqueue(services: Services, *, mode: str, payload: dict[str, Any]) -> JobRecord:
    workspace = services.repository.require_workspace(
        st.session_state.session_id, st.session_state.workspace_id
    )
    job = services.jobs.enqueue(
        session_id=st.session_state.session_id,
        workspace_id=st.session_state.workspace_id,
        conversation_id=st.session_state.conversation_id,
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
        dedupe_key=new_dedupe_key(),
    )
    st.session_state.active_job_id = job.job_id
    st.session_state.current_result_mode = mode
    st.session_state.pop("last_failure", None)
    st.session_state.pop("last_completion", None)
    st.session_state.setdefault("last_outcomes", {}).pop(mode, None)
    st.session_state.setdefault("last_outcome_action_ids", {}).pop(mode, None)
    services.scheduler.wake()
    return job


def _render_job_status(services: Services) -> None:
    job_id = st.session_state.get("active_job_id")
    if not job_id:
        completion = st.session_state.get("last_completion")
        if completion:
            st.success(f"完了（操作ID {completion['action_id'][:8]}）")
        failure = st.session_state.get("last_failure")
        if failure:
            st.error(
                f"この操作は完了しませんでした：{failure['code']} "
                f"（操作ID {failure['action_id'][:8]}）。代替回答は表示していません。"
            )
        return
    try:
        job = services.jobs.get(session_id=st.session_state.session_id, job_id=job_id)
    except Exception as exc:
        code, message = safe_error(exc)
        st.error(f"{message}（{code}）")
        return
    if job.state is JobState.QUEUED:
        st.info(f"待機中：前に {job.queue_position or 0} 件（操作ID {job.action_id[:8]}）")
        if st.button("この待機を取り消す", key=f"cancel-{job.job_id}"):
            services.jobs.cancel(session_id=job.session_id, job_id=job.job_id)
            st.rerun(scope="fragment")
        return
    if job.state is JobState.RUNNING:
        st.info(f"実行中（操作ID {job.action_id[:8]}）。再送せず、そのままお待ちください。")
        if st.button("取消を要求する", key=f"cancel-{job.job_id}"):
            services.jobs.cancel(session_id=job.session_id, job_id=job.job_id)
            st.rerun(scope="fragment")
        return
    if job.state is JobState.CANCEL_REQUESTED:
        st.warning("取消要求中です。送信済みAPIの利用量は確定まで不明な場合があります。")
        return
    if job.state is JobState.SUCCEEDED:
        outcome = services.repository.get_action_outcome(
            job.workspace_id, job.action_id, session_id=job.session_id
        )
        if outcome is None:
            st.error("成果の確定状態を確認できません（outcome_missing）。")
            return
        outcomes = st.session_state.setdefault("last_outcomes", {})
        outcomes[job.mode] = outcome["payload"]
        st.session_state.setdefault("last_outcome_action_ids", {})[job.mode] = job.action_id
        st.session_state["last_completion"] = {"action_id": job.action_id}
        st.session_state.pop("last_failure", None)
    else:
        st.session_state.pop("last_completion", None)
        st.session_state["last_failure"] = {
            "mode": job.mode,
            "action_id": job.action_id,
            "code": job.safe_error_code or job.state.value,
        }
    st.session_state.active_job_id = None
    st.rerun(scope="app")


if os.environ.get("APP_ENV") == "test":
    render_job_status = st.fragment(_render_job_status)
else:
    render_job_status = st.fragment(_render_job_status, run_every="2s")


def show_action_error(exc: BaseException) -> None:
    code, message = safe_error(exc)
    st.error(f"{message}（{code}）")
