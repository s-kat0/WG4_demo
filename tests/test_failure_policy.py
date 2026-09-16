from __future__ import annotations

from pathlib import Path

import pytest

from wg4_demo.errors import SearchFailure
from wg4_demo.repository import Repository
from wg4_demo.retrieval import RetrievalService
from wg4_demo.schemas import SessionRecord


def test_search_zero_and_search_failure_are_distinct(
    repository: Repository, participant: SessionRecord, project_root: Path, monkeypatch
) -> None:
    workspace = repository.create_workspace(
        participant.id,
        seed_mode="approved_v1",
        seed_path=project_root / "data" / "approved_seed.json",
    )
    retrieval = RetrievalService(repository, project_root / "data" / "vocabulary.json")
    zero = retrieval.search_knowledge(workspace.id, "冷却器999")
    assert zero.status == "success"
    assert zero.hits == []

    def fail(_workspace_id: str):
        raise RuntimeError("injected database outage")

    monkeypatch.setattr(repository, "list_knowledge", fail)
    with pytest.raises(SearchFailure):
        retrieval.search_knowledge(workspace.id, "冷却器1")
