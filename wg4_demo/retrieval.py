"""Deterministic, workspace-scoped lexical retrieval."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from wg4_demo.database import connect_sqlite
from wg4_demo.errors import SearchFailure
from wg4_demo.repository import Repository
from wg4_demo.schemas import ConditionScope, FactKind, SearchHit, SearchSuccess


def normalize_text(text: str, aliases: dict[str, str]) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    for alias in sorted(aliases, key=len, reverse=True):
        normalized = normalized.replace(alias, aliases[alias])
    return re.sub(r"\s+", "", normalized).lower()


def bigrams(text: str) -> set[str]:
    compact = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]", "", text)
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


class RetrievalService:
    def __init__(self, repository: Repository, vocabulary_path: Path) -> None:
        self.repository = repository
        payload = json.loads(vocabulary_path.read_text(encoding="utf-8"))
        self.aliases: dict[str, str] = payload["aliases"]

    def search_knowledge(
        self,
        workspace_id: str,
        query: str,
        *,
        equipment_name: str | None = None,
        limit: int = 5,
    ) -> SearchSuccess:
        if not query or len(query) > 1500:
            raise ValueError("query must contain 1-1500 characters")
        try:
            items = self.repository.list_knowledge(workspace_id)
            workspace_revision = self._workspace_revision(workspace_id)
        except Exception as exc:
            raise SearchFailure() from exc
        normalized_query = normalize_text(query, self.aliases)
        explicit_equipment = self._equipment_from_query(normalized_query)
        requested_equipment = equipment_name or explicit_equipment
        if requested_equipment:
            requested_equipment = normalize_text(requested_equipment, self.aliases)
        query_grams = bigrams(normalized_query)
        hits: list[SearchHit] = []
        for item in items:
            normalized_equipment = normalize_text(item.equipment, self.aliases)
            if requested_equipment and normalized_equipment != requested_equipment:
                continue
            searchable = " ".join(
                [item.equipment, item.case_label or "", *(f.text for f in item.facts)]
            )
            item_grams = bigrams(normalize_text(searchable, self.aliases))
            overlap = len(query_grams & item_grams) / max(len(query_grams), 1)
            equipment_bonus = 0.35 if explicit_equipment == normalized_equipment else 0.0
            fact_ids = [fact.id for fact in item.facts]
            evidence_ids = list(
                dict.fromkeys(ref.segment_id for fact in item.facts for ref in fact.evidence_refs)
            )
            condition_matches = {
                fact.id: self._match_condition(normalized_query, fact.text)
                for fact in item.facts
                if fact.kind is FactKind.CONDITION
                and fact.condition_scope is ConditionScope.CASE_CONTEXT
            }
            matched = sum(value == "matched" for value in condition_matches.values())
            contradicted = sum(value == "contradicted" for value in condition_matches.values())
            condition_score = matched * 0.12 - contradicted * 0.3
            score = overlap + equipment_bonus + condition_score
            if score > 0:
                hits.append(
                    SearchHit(
                        knowledge_id=item.id,
                        display_name=item.display_name,
                        version=item.version,
                        equipment=item.equipment,
                        case_label=item.case_label,
                        score=round(score, 6),
                        fact_ids=fact_ids,
                        evidence_segment_ids=evidence_ids[:6],
                        condition_matches=condition_matches,
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.display_name))
        return SearchSuccess(
            hits=hits[:limit], executed_at=datetime.now(UTC), kb_revision=workspace_revision
        )

    def search_source_segments(
        self, workspace_id: str, query: str, *, limit: int = 6
    ) -> list[dict[str, object]]:
        try:
            allowed = self.repository.allowed_evidence_ids(workspace_id)
            segments = (
                self.repository.read_segments(workspace_id, sorted(allowed)) if allowed else []
            )
        except Exception as exc:
            raise SearchFailure("原文検索に失敗したため、根拠の有無を判定できません。") from exc
        query_grams = bigrams(normalize_text(query, self.aliases))
        scored: list[tuple[float, dict[str, object]]] = []
        for segment in segments:
            score = len(
                query_grams & bigrams(normalize_text(str(segment["text"]), self.aliases))
            ) / max(len(query_grams), 1)
            if score > 0:
                scored.append((score, segment))
        scored.sort(key=lambda pair: -pair[0])
        return [dict(item, score=round(score, 6)) for score, item in scored[:limit]]

    def _workspace_revision(self, workspace_id: str) -> int:
        connection = connect_sqlite(self.repository.path)
        try:
            row = connection.execute(
                "SELECT kb_revision FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(workspace_id)
        return int(row["kb_revision"])

    def _equipment_from_query(self, normalized_query: str) -> str | None:
        matches = set(re.findall(r"冷却器\d+", normalized_query))
        if len(matches) > 1:
            return "__ambiguous_equipment__"
        return next(iter(matches), None)

    def _match_condition(
        self, query: str, condition: str
    ) -> Literal["matched", "contradicted", "unknown"]:
        normalized_condition = normalize_text(condition, self.aliases)
        if "交換直後" in normalized_condition:
            return "matched" if "交換直後" in query or "取り替えたばかり" in query else "unknown"
        if "流量" in normalized_condition and "低下" in normalized_condition:
            if "流量低下" in query or "流量が低" in query:
                return "matched"
            if "流量" in query and ("通常範囲" in query or "通常" in query):
                return "contradicted"
            return "unknown"
        if "流量" in normalized_condition and (
            "通常範囲" in normalized_condition or "通常" in normalized_condition
        ):
            if "流量" in query and ("通常範囲" in query or "通常" in query):
                return "matched"
            if "流量低下" in query or "流量が低" in query:
                return "contradicted"
            return "unknown"
        if "入口温度" in normalized_condition and (
            "通常範囲" in normalized_condition or "通常" in normalized_condition
        ):
            return "matched" if "入口温度" in query and "通常" in query else "unknown"
        if normalized_condition and normalized_condition in query:
            return "matched"
        return "unknown"
