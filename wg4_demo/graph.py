"""NetworkX traversal and safe Graphviz rendering from approved facts."""

from __future__ import annotations

from dataclasses import dataclass

import graphviz
import networkx as nx

from wg4_demo.repository import KnowledgeRecord, Repository
from wg4_demo.schemas import ConditionScope


@dataclass(frozen=True, slots=True)
class GraphResult:
    knowledge_id: str
    version: int
    facts: list[dict[str, object]]
    nodes: list[dict[str, str]]
    edges: list[dict[str, str]]
    dot: str


class GraphService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def get_context(self, workspace_id: str, knowledge_id: str, version: int) -> GraphResult:
        item = self.repository.get_knowledge(workspace_id, knowledge_id)
        if item.version != version:
            raise ValueError("only the active version is available to QA")
        graph = self._build(item)
        descendants = nx.descendants(graph, self._item_node(item))
        subgraph = graph.subgraph({self._item_node(item), *descendants}).copy()
        nodes = [
            {"id": node, "type": data["type"], "label": data["label"]}
            for node, data in subgraph.nodes(data=True)
        ]
        edges = [
            {"source": source, "target": target, "relation": data["relation"]}
            for source, target, data in subgraph.edges(data=True)
        ]
        facts: list[dict[str, object]] = [
            {
                "fact_id": fact.id,
                "kind": fact.kind.value,
                "text": fact.text,
                "condition_scope": (
                    fact.condition_scope.value if fact.condition_scope is not None else None
                ),
                "parent_action_fact_id": fact.parent_action_fact_id,
                "evidence_segment_ids": [ref.segment_id for ref in fact.evidence_refs],
            }
            for fact in item.facts
        ]
        return GraphResult(
            item.id,
            item.version,
            facts,
            nodes,
            edges,
            self._to_dot(subgraph),
        )

    def _build(self, item: KnowledgeRecord) -> nx.DiGraph:
        graph = nx.DiGraph()
        item_node = self._item_node(item)
        graph.add_node(
            item_node, type="KnowledgeItem", label=f"{item.display_name} v{item.version}"
        )
        equipment_node = f"equipment:{item.equipment}"
        graph.add_node(equipment_node, type="Equipment", label=item.equipment)
        graph.add_edge(item_node, equipment_node, relation="about_equipment")
        if item.case_label:
            case_node = f"case:{item.case_label}"
            graph.add_node(case_node, type="Case", label=item.case_label)
            graph.add_edge(item_node, case_node, relation="about_case")
        segments = {
            segment["id"]: segment
            for segment in self.repository.read_segments(
                item.workspace_id,
                list(
                    dict.fromkeys(
                        ref.segment_id for fact in item.facts for ref in fact.evidence_refs
                    )
                ),
            )
        }
        for fact in item.facts:
            fact_node = f"fact:{fact.id}"
            graph.add_node(fact_node, type="Fact", label=fact.text)
            graph.add_edge(item_node, fact_node, relation="has_fact")
            for evidence in fact.evidence_refs:
                segment = segments[evidence.segment_id]
                segment_node = f"segment:{evidence.segment_id}"
                graph.add_node(segment_node, type="SourceSegment", label=str(segment["text"]))
                graph.add_edge(fact_node, segment_node, relation="supported_by")
                source_node = f"source:{segment['source_id']}"
                graph.add_node(source_node, type="Source", label=str(segment["title"]))
                graph.add_edge(segment_node, source_node, relation="part_of")
            if (
                fact.condition_scope is ConditionScope.ACTION_PREREQUISITE
                and fact.parent_action_fact_id
            ):
                graph.add_edge(
                    fact_node,
                    f"fact:{fact.parent_action_fact_id}",
                    relation="prerequisite_for",
                )
        return graph

    def _to_dot(self, graph: nx.DiGraph) -> str:
        dot = graphviz.Digraph("knowledge")
        dot.attr(rankdir="LR")
        for index, (node_id, data) in enumerate(graph.nodes(data=True)):
            safe_id = f"n{index}"
            graph.nodes[node_id]["dot_id"] = safe_id
            dot.node(safe_id, label=graphviz.escape(str(data["label"])))
        for source, target, data in graph.edges(data=True):
            dot.edge(
                graph.nodes[source]["dot_id"],
                graph.nodes[target]["dot_id"],
                label=graphviz.escape(str(data["relation"])),
            )
        return str(dot.source)

    def _item_node(self, item: KnowledgeRecord) -> str:
        return f"knowledge:{item.id}:v{item.version}"
