from __future__ import annotations

from pathlib import Path

from wg4_demo.repository import Repository
from wg4_demo.retrieval import RetrievalService
from wg4_demo.schemas import SessionRecord


def test_main_question_prefers_item3_and_separates_contradicted_condition(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace = repository.create_workspace(
        participant.id,
        seed_mode="approved_v1",
        seed_path=project_root / "data" / "approved_seed.json",
    )
    retrieval = RetrievalService(repository, project_root / "data" / "vocabulary.json")

    result = retrieval.search_knowledge(
        workspace.id,
        "冷却器1の出口温度の表示が高い。温度計交換直後で、冷却水流量と入口温度は通常範囲。何を確認するか？",
    )

    assert result.status == "success"
    assert result.hits[0].display_name == "知識項目3"
    item2 = next(hit for hit in result.hits if hit.display_name == "知識項目2")
    assert "contradicted" in item2.condition_matches.values()


def test_unknown_equipment_returns_successful_zero_results(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace = repository.create_workspace(
        participant.id,
        seed_mode="approved_v1",
        seed_path=project_root / "data" / "approved_seed.json",
    )
    retrieval = RetrievalService(repository, project_root / "data" / "vocabulary.json")

    result = retrieval.search_knowledge(workspace.id, "冷却器9の記録")

    assert result.status == "success"
    assert result.hits == []


def test_alias_and_full_width_number_do_not_merge_other_equipment(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace = repository.create_workspace(
        participant.id,
        seed_mode="approved_v1",
        seed_path=project_root / "data" / "approved_seed.json",
    )
    retrieval = RetrievalService(repository, project_root / "data" / "vocabulary.json")

    result = retrieval.search_knowledge(workspace.id, "冷却器１でCW流量は平常")

    assert result.hits
    assert all(hit.equipment == "冷却器1" for hit in result.hits)
