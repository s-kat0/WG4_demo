"""Fixed-size process-local workers consuming the SQLite queue."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from wg4_demo.errors import AppError
from wg4_demo.jobs import JobRecord, JobService
from wg4_demo.repository import Repository
from wg4_demo.schemas import JobState

JobHandler = Callable[[JobRecord], tuple[str, dict[str, object]]]


class Scheduler:
    def __init__(
        self,
        jobs: JobService,
        repository: Repository,
        handlers: dict[str, JobHandler],
        *,
        max_workers: int,
    ) -> None:
        self.jobs = jobs
        self.repository = repository
        self.handlers = handlers
        self.max_workers = max_workers
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._coordinator: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None

    def start(self) -> None:
        if self._coordinator is not None and self._coordinator.is_alive():
            return
        self.jobs.recover_interrupted_jobs()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="wg4-worker"
        )
        self._coordinator = threading.Thread(
            target=self._coordinate, name="wg4-coordinator", daemon=True
        )
        self._coordinator.start()

    def wake(self) -> None:
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._coordinator:
            self._coordinator.join(timeout=5)
        if self._executor:
            self._executor.shutdown(wait=True, cancel_futures=False)

    def _coordinate(self) -> None:
        assert self._executor is not None
        while not self._stop_event.is_set():
            claimed = False
            for index in range(self.max_workers):
                job = self.jobs.claim_next(f"worker-{index}")
                if job is None:
                    break
                claimed = True
                self._executor.submit(self._execute, job)
            if not claimed:
                self._wake_event.wait(timeout=0.1)
                self._wake_event.clear()

    def _execute(self, job: JobRecord) -> None:
        try:
            if not self.jobs.heartbeat(job):
                self.jobs.fail(
                    job,
                    state=JobState.EXPIRED,
                    safe_error_code="action_expired_before_run",
                    failure_stage="worker",
                )
                return
            handler = self.handlers[job.mode]
            outcome_type, payload = handler(job)
            if job.run_deadline_at is not None and datetime.now(UTC) >= job.run_deadline_at:
                self.jobs.fail(
                    job,
                    state=JobState.EXPIRED,
                    safe_error_code="action_timeout",
                    failure_stage="worker",
                )
                return
            outcome_id = self.repository.save_action_outcome(
                job.workspace_id,
                action_id=job.action_id,
                session_id=job.session_id,
                claim_token=job.claim_token or "",
                input_sha256=job.input_sha256,
                kb_revision=job.kb_revision,
                outcome_type=outcome_type,
                payload=payload,
            )
            self.jobs.succeed(job, outcome_id)
        except AppError as exc:
            state = JobState.INDETERMINATE if exc.indeterminate else JobState.FAILED
            if exc.code == "stale_context":
                state = JobState.STALE_CONTEXT
            if exc.code.startswith("job_cancelled"):
                state = JobState.CANCELLED
            self.jobs.fail(
                job,
                state=state,
                safe_error_code=exc.code,
                failure_stage=exc.stage,
            )
        except BaseException:
            self.jobs.fail(
                job,
                state=JobState.FAILED,
                safe_error_code="internal_error",
                failure_stage="job_boundary",
            )
        finally:
            self.wake()
