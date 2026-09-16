from __future__ import annotations

import threading
import time

import pytest

from wg4_demo.auth import AuthService
from wg4_demo.errors import AppError, AuthorizationError
from wg4_demo.jobs import JobService
from wg4_demo.repository import Repository
from wg4_demo.scheduler import Scheduler
from wg4_demo.schemas import JobState, Role
from wg4_demo.settings import Settings


def enqueue_job(
    jobs: JobService,
    repository: Repository,
    session_id: str,
    workspace_id: str,
    *,
    dedupe_key: str,
):
    conversation = repository.create_conversation(workspace_id)
    revision = repository.require_workspace(session_id, workspace_id).kb_revision
    return jobs.enqueue(
        session_id=session_id,
        workspace_id=workspace_id,
        conversation_id=conversation,
        mode="fake",
        payload={"value": dedupe_key},
        kb_revision=revision,
        model_id="test-model",
        model_settings={"store": False},
        prompt_version="test-v1",
        schema_version="test-v1",
        dedupe_key=dedupe_key,
    )


def test_duplicate_submit_returns_same_job_and_owner_only(
    settings: Settings, auth: AuthService, repository: Repository
) -> None:
    first_session = auth.login("participant-secret", role=Role.PARTICIPANT, client_token="queue-a")
    second_session = auth.login("participant-secret", role=Role.PARTICIPANT, client_token="queue-b")
    first_workspace = repository.create_workspace(first_session.id, seed_mode="from_scratch")
    repository.create_workspace(second_session.id, seed_mode="from_scratch")
    jobs = JobService(settings.control_db_path, settings, auth, repository)

    first = enqueue_job(
        jobs, repository, first_session.id, first_workspace.id, dedupe_key="button-submit-1"
    )
    duplicate = jobs.enqueue(
        session_id=first_session.id,
        workspace_id=first_workspace.id,
        conversation_id=first.conversation_id,
        mode="fake",
        payload={"value": "button-submit-1"},
        kb_revision=first.kb_revision,
        model_id="test-model",
        model_settings={"store": False},
        prompt_version="test-v1",
        schema_version="test-v1",
        dedupe_key="button-submit-1",
    )
    assert duplicate.job_id == first.job_id
    with pytest.raises(AuthorizationError):
        jobs.get(session_id=second_session.id, job_id=first.job_id)
    with pytest.raises(AppError, match="session_job_active"):
        enqueue_job(
            jobs, repository, first_session.id, first_workspace.id, dedupe_key="another-action"
        )


def test_thirty_sessions_never_exceed_three_workers(
    settings: Settings, auth: AuthService, repository: Repository
) -> None:
    jobs = JobService(settings.control_db_path, settings, auth, repository)
    submitted: list[tuple[str, str]] = []
    for index in range(30):
        session = auth.login(
            "participant-secret",
            role=Role.PARTICIPANT,
            client_token=f"load-{index}",
        )
        workspace = repository.create_workspace(session.id, seed_mode="from_scratch")
        job = enqueue_job(jobs, repository, session.id, workspace.id, dedupe_key=f"load-{index}")
        submitted.append((session.id, job.job_id))

    lock = threading.Lock()
    active = 0
    max_active = 0
    seen: set[str] = set()

    def handler(job):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            assert job.action_id not in seen
            seen.add(job.action_id)
        time.sleep(0.03)
        with lock:
            active -= 1
        return "fake_result", {"job": job.job_id}

    scheduler = Scheduler(jobs, repository, {"fake": handler}, max_workers=3)
    scheduler.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            states = [
                jobs.get(session_id=session, job_id=job_id).state for session, job_id in submitted
            ]
            if all(state is JobState.SUCCEEDED for state in states):
                break
            time.sleep(0.03)
        assert all(
            jobs.get(session_id=session, job_id=job_id).state is JobState.SUCCEEDED
            for session, job_id in submitted
        )
    finally:
        scheduler.stop()
    assert len(seen) == 30
    assert max_active <= 3


def test_queued_cancel_consumes_no_api_or_worker(
    settings: Settings, auth: AuthService, repository: Repository
) -> None:
    session = auth.login("participant-secret", role=Role.PARTICIPANT, client_token="cancel-queued")
    workspace = repository.create_workspace(session.id, seed_mode="from_scratch")
    jobs = JobService(settings.control_db_path, settings, auth, repository)
    job = enqueue_job(jobs, repository, session.id, workspace.id, dedupe_key="cancel-me")

    cancelled = jobs.cancel(session_id=session.id, job_id=job.job_id)

    assert cancelled.state is JobState.CANCELLED


def test_running_cancel_discards_late_result(
    settings: Settings, auth: AuthService, repository: Repository
) -> None:
    session = auth.login("participant-secret", role=Role.PARTICIPANT, client_token="cancel-running")
    workspace = repository.create_workspace(session.id, seed_mode="from_scratch")
    jobs = JobService(settings.control_db_path, settings, auth, repository)
    job = enqueue_job(jobs, repository, session.id, workspace.id, dedupe_key="late")
    started = threading.Event()
    release = threading.Event()

    def handler(_job):
        started.set()
        release.wait(timeout=3)
        return "late_result", {"should": "not publish"}

    scheduler = Scheduler(jobs, repository, {"fake": handler}, max_workers=1)
    scheduler.start()
    try:
        assert started.wait(timeout=3)
        cancelling = jobs.cancel(session_id=session.id, job_id=job.job_id)
        assert cancelling.state is JobState.CANCEL_REQUESTED
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = jobs.get(session_id=session.id, job_id=job.job_id)
            if result.state is JobState.CANCELLED:
                break
            time.sleep(0.02)
        assert jobs.get(session_id=session.id, job_id=job.job_id).state is JobState.CANCELLED
        assert (
            repository.get_action_outcome(workspace.id, job.action_id, session_id=session.id)
            is None
        )
    finally:
        release.set()
        scheduler.stop()
