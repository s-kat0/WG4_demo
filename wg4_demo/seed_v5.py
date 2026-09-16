"""Strict validation for the synthetic v5 seed manifest.

The proposed JSON is not a database dump.  This module validates it before the
repository maps fixture keys to workspace-local identifiers in one transaction.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from wg4_demo.errors import ValidationFailure
from wg4_demo.schemas import CauseStatus, ConditionScope, FactKind, StrictModel


class SeedImportContract(StrictModel):
    only_new_workspaces: Literal[True]
    assign_runtime_ids_on_server: Literal[True]
    preserve_old_workspaces: Literal[True]
    verify_quotes_and_scopes: Literal[True]
    seed_actor: Literal["synthetic_fixture_author"]
    source_question_is_not_fact_evidence: Literal[True]
    status_label: str
    llm_calls_required: Literal[0]


class SeedSegment(StrictModel):
    key: str = Field(min_length=1, max_length=128)
    speaker: Literal["document", "operator", "assistant"]
    text: str = Field(min_length=1, max_length=2000)


class SeedSource(StrictModel):
    key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["document", "interview"]
    equipment_label: str = Field(min_length=1, max_length=100)
    case_label: str | None = Field(default=None, max_length=100)
    synthetic: Literal[True]
    segments: list[SeedSegment] = Field(min_length=1, max_length=12)


class SeedEvidence(StrictModel):
    segment_key: str = Field(min_length=1, max_length=128)
    quote: str = Field(min_length=1, max_length=500)


class SeedFact(StrictModel):
    key: str = Field(min_length=1, max_length=128)
    kind: FactKind
    text: str = Field(min_length=1, max_length=200)
    condition_scope: ConditionScope | None = None
    evidence: list[SeedEvidence] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_scope(self) -> SeedFact:
        if (self.kind is FactKind.CONDITION) != (self.condition_scope is not None):
            raise ValueError("condition_scope must be present only for condition facts")
        if self.condition_scope is ConditionScope.ACTION_PREREQUISITE:
            raise ValueError("the initial fixture cannot contain future action prerequisites")
        return self


class SeedKnowledgeItem(StrictModel):
    key: str = Field(min_length=1, max_length=128)
    display_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    equipment_label: str = Field(min_length=1, max_length=100)
    case_label: str | None = Field(default=None, max_length=100)
    source_kind: Literal["document", "interview"]
    source_keys: list[str] = Field(min_length=1, max_length=6)
    tags: list[str] = Field(max_length=12)
    cause_status: CauseStatus
    facts: list[SeedFact] = Field(min_length=1, max_length=12)
    missing_fields: list[str] = Field(max_length=12)
    registration_origin: Literal["synthetic_fixture"]
    display_origin_label: str = Field(min_length=1, max_length=100)


class V5SeedManifest(StrictModel):
    fixture_schema: Literal["wg4-seed-manifest-v5-proposal"]
    fixture_version: Literal["wg4-practical-seed-v5"]
    purpose: str
    synthetic: Literal[True]
    review_notice: str
    import_contract: SeedImportContract
    equipments: list[str]
    sources: list[SeedSource]
    knowledge_items: list[SeedKnowledgeItem]

    @model_validator(mode="after")
    def validate_manifest(self) -> V5SeedManifest:
        if len(self.sources) != 12 or len(self.knowledge_items) != 12:
            raise ValueError("v5 requires 12 sources and 12 independent knowledge items")
        if Counter(item.source_kind for item in self.knowledge_items) != {
            "document": 6,
            "interview": 6,
        }:
            raise ValueError("v5 requires six document and six Q&A items")
        if Counter(item.equipment_label for item in self.knowledge_items) != {
            "冷却器1": 8,
            "ポンプ1": 2,
            "貯槽1": 2,
        }:
            raise ValueError("unexpected equipment counts")
        if len({source.key for source in self.sources}) != len(self.sources):
            raise ValueError("duplicate source key")
        if len({item.key for item in self.knowledge_items}) != len(self.knowledge_items):
            raise ValueError("duplicate knowledge key")
        if len({item.display_number for item in self.knowledge_items}) != len(self.knowledge_items):
            raise ValueError("duplicate display number")

        sources = {source.key: source for source in self.sources}
        segments = {
            segment.key: (source.key, segment)
            for source in self.sources
            for segment in source.segments
        }
        if len(segments) != sum(len(source.segments) for source in self.sources):
            raise ValueError("duplicate segment key")
        fact_keys: list[str] = []
        for item in self.knowledge_items:
            if any(source_key not in sources for source_key in item.source_keys):
                raise ValueError("knowledge item refers to an unknown source")
            if any(sources[key].kind != item.source_kind for key in item.source_keys):
                raise ValueError("source kind does not match the knowledge item")
            allowed_segments = {
                segment.key
                for source_key in item.source_keys
                for segment in sources[source_key].segments
            }
            for fact in item.facts:
                fact_keys.append(fact.key)
                for evidence in fact.evidence:
                    if evidence.segment_key not in allowed_segments:
                        raise ValueError("fact evidence is outside its source set")
                    _, segment = segments[evidence.segment_key]
                    if segment.speaker == "assistant":
                        raise ValueError("an AI question cannot be fact evidence")
                    if segment.text.count(evidence.quote) != 1:
                        raise ValueError("evidence quote must occur exactly once")
        if len(fact_keys) != len(set(fact_keys)):
            raise ValueError("duplicate fact key")
        if len(segments) != 35 or len(fact_keys) != 41:
            raise ValueError("unexpected v5 segment or fact count")
        return self


def load_v5_seed(path: Path) -> V5SeedManifest:
    """Load and fully validate the synthetic v5 manifest without network access."""

    try:
        raw = path.read_text(encoding="utf-8")
        manifest = V5SeedManifest.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise ValidationFailure(
            "初期12件の教材seedを検証できないため、新規領域を作成しません。",
            code="seed_v5_invalid",
        ) from exc
    for future_term in (
        "温度計の交換後",
        "温度計交換",
        "校正",
        "表示の変化なのか実際の温度の変化なのか",
    ):
        if future_term in raw:
            raise ValidationFailure(
                "講演中に追加する情報が初期seedへ混入しています。",
                code="seed_v5_future_content",
            )
    return manifest
