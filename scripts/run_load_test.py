"""Run a 30-session delayed-fake queue test outside the normal app path."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

from argon2 import PasswordHasher

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wg4_demo.auth import AuthService
from wg4_demo.jobs import JobService
from wg4_demo.repository import Repository
from wg4_demo.scheduler import Scheduler
from wg4_demo.schemas import JobState, Role
from wg4_demo.settings import settings_from_mapping


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.05)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="wg4-load-") as temp:
        runtime = Path(temp)
        hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
        settings = settings_from_mapping(
            {
                "APP_ENV": "test",
                "OPENAI_MODEL": "fake-model",
                "DEMO_PASSWORD_HASH": hasher.hash("load-test-password"),
                "ADMIN_PASSWORD_HASH": hasher.hash("different-admin-password"),
                "AUTH_VERSION": "load-v1",
                "DEMO_EXPIRES_AT": "2099-01-01T00:00:00+00:00",
                "GLOBAL_TPM": "100000",
            },
            runtime_dir=runtime,
        )
        auth = AuthService(settings.control_db_path, settings)
        repository = Repository(settings.knowledge_db_path)
        jobs = JobService(settings.control_db_path, settings, auth, repository)
        submitted: list[tuple[str, str, float]] = []
        all_knowledge_ids: set[str] = set()
        for index in range(args.sessions):
            session = auth.login(
                "load-test-password",
                role=Role.PARTICIPANT,
                client_token=f"context-{index}",
            )
            workspace = repository.create_workspace(
                session.id,
                seed_mode="practical_v5",
                seed_path=root / "data" / "knowledge_seed_v5.json",
            )
            items = repository.list_knowledge(workspace.id)
            if len(items) != 12:
                raise RuntimeError("v5 seed count mismatch during load test")
            workspace_ids = {item.id for item in items}
            if all_knowledge_ids & workspace_ids:
                raise RuntimeError("knowledge ids leaked across workspaces")
            all_knowledge_ids.update(workspace_ids)
            conversation = repository.create_conversation(workspace.id)
            started = time.monotonic()
            job = jobs.enqueue(
                session_id=session.id,
                workspace_id=workspace.id,
                conversation_id=conversation,
                mode="fake",
                payload={"index": index},
                kb_revision=workspace.kb_revision,
                model_id="fake-model",
                model_settings={"fake": True},
                prompt_version="load-v1",
                schema_version="load-v1",
                dedupe_key=f"load-{index}",
            )
            submitted.append((session.id, job.job_id, started))
        lock = threading.Lock()
        active = 0
        maximum = 0

        def fake_handler(job):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(args.delay)
            with lock:
                active -= 1
            return "fake_result", {"job_id": job.job_id}

        scheduler = Scheduler(jobs, repository, {"fake": fake_handler}, max_workers=3)
        scheduler.start()
        completion: dict[str, float] = {}
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and len(completion) < len(submitted):
                for session_id, job_id, started in submitted:
                    if job_id in completion:
                        continue
                    if jobs.get(session_id=session_id, job_id=job_id).state is JobState.SUCCEEDED:
                        completion[job_id] = time.monotonic() - started
                time.sleep(0.01)
        finally:
            scheduler.stop()
        latencies = sorted(completion.values())
        if len(latencies) != args.sessions:
            raise RuntimeError("not all fake jobs completed")
        p95_index = max(0, round(0.95 * len(latencies)) - 1)
        report = {
            "kind": "delayed_fake_queue_test",
            "sessions": args.sessions,
            "delay_seconds": args.delay,
            "completed": len(latencies),
            "failed": 0,
            "max_concurrent": maximum,
            "median_seconds": round(statistics.median(latencies), 4),
            "p95_seconds": round(latencies[p95_index], 4),
            "max_seconds": round(max(latencies), 4),
            "external_api_calls": 0,
            "seed_items_per_workspace": 12,
            "workspace_knowledge_ids_disjoint": True,
            "project_root": root.name,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
