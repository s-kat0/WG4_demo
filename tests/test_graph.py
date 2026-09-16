from __future__ import annotations

from pathlib import Path

from wg4_demo.graph import GraphService
from wg4_demo.repository import Repository
from wg4_demo.schemas import SessionRecord


def test_graph_is_built_from_approved_version_and_has_evidence_path(
    repository: Repository, participant: SessionRecord, project_root: Path
) -> None:
    workspace = repository.create_workspace(
        participant.id,
        seed_mode="approved_v1",
        seed_path=project_root / "data" / "approved_seed.json",
    )
    item = repository.list_knowledge(workspace.id)[2]

    graph = GraphService(repository).get_context(workspace.id, item.id, item.version)

    relations = {edge["relation"] for edge in graph.edges}
    assert {"has_fact", "supported_by", "part_of"}.issubset(relations)
    action = next(fact for fact in graph.facts if fact["kind"] == "check_action")
    assert action["fact_id"]
    assert action["evidence_segment_ids"]
    conditions = [fact for fact in graph.facts if fact["kind"] == "condition"]
    assert conditions
    assert all(fact["condition_scope"] for fact in conditions)
    assert "知識項目3 v1" in graph.dot
