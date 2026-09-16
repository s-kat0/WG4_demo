"""Strict public schemas. LLM outputs never contain server-owned identifiers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Role(StrEnum):
    PARTICIPANT = "participant"
    ADMIN = "admin"


class FactKind(StrEnum):
    OBSERVATION = "observation"
    CHECK_ACTION = "check_action"
    CONDITION = "condition"
    CAUSE_HYPOTHESIS = "cause_hypothesis"
    DECISION_REASON = "decision_reason"
    EXCEPTION = "exception"
    CAUSE_STATUS = "cause_status"


class ConditionScope(StrEnum):
    CASE_CONTEXT = "case_context"
    APPLICABILITY = "applicability"
    ACTION_PREREQUISITE = "action_prerequisite"
    EXCLUSION = "exclusion"


class CauseStatus(StrEnum):
    UNRESOLVED = "unresolved"
    HYPOTHESIS = "hypothesis"
    CONFIRMED_IN_SOURCE = "confirmed_in_source"


class EvidenceDraft(StrictModel):
    segment_id: str = Field(min_length=1, max_length=128)
    quote: str = Field(min_length=1, max_length=500)


class EvidenceRef(EvidenceDraft):
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered_offsets(self) -> EvidenceRef:
        if self.end_char <= self.start_char:
            raise ValueError("evidence end must be after start")
        return self


class FactDraft(StrictModel):
    kind: FactKind
    text: str = Field(min_length=1, max_length=200)
    condition_scope: ConditionScope | None = None
    parent_action_fact_id: str | None = None
    evidence: list[EvidenceDraft] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def condition_scope_matches_kind(self) -> FactDraft:
        if self.kind is FactKind.CONDITION and self.condition_scope is None:
            raise ValueError("condition facts require condition_scope")
        if self.kind is not FactKind.CONDITION and self.condition_scope is not None:
            raise ValueError("only condition facts may have condition_scope")
        if (
            self.condition_scope is ConditionScope.ACTION_PREREQUISITE
            and not self.parent_action_fact_id
        ):
            raise ValueError("action prerequisite requires parent action")
        return self


class KnowledgeDraft(StrictModel):
    facts: list[FactDraft] = Field(min_length=1, max_length=12)
    cause_status: CauseStatus
    missing_fields: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(max_length=12)


class InterviewQuestion(StrictModel):
    question: str = Field(min_length=1, max_length=300)
    related_missing_field: str | None = Field(default=None, max_length=100)


class StoredFact(StrictModel):
    id: str
    kind: FactKind
    text: str = Field(min_length=1, max_length=200)
    condition_scope: ConditionScope | None = None
    parent_action_fact_id: str | None = None
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=4)


class OperationType(StrEnum):
    ADD_FACT = "add_fact"
    REPLACE_FACT = "replace_fact"
    REMOVE_FACT = "remove_fact"


class ProposalOperation(StrictModel):
    operation: OperationType
    target_fact_id: str | None = None
    new_fact: FactDraft | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ProposalOperation:
        if self.operation is OperationType.ADD_FACT:
            if self.target_fact_id is not None or self.new_fact is None:
                raise ValueError("add_fact accepts only new_fact")
        elif self.operation is OperationType.REPLACE_FACT:
            if self.target_fact_id is None or self.new_fact is None:
                raise ValueError("replace_fact requires target and new fact")
        elif self.target_fact_id is None or self.new_fact is not None:
            raise ValueError("remove_fact accepts only target_fact_id")
        return self


class AddFactToolOperation(StrictModel):
    operation: Literal[OperationType.ADD_FACT]
    new_fact: FactDraft


class AddActionPrerequisiteToolOperation(StrictModel):
    operation: Literal["add_action_prerequisite"]
    text: str = Field(min_length=1, max_length=200)
    parent_action_fact_id: str
    evidence: list[EvidenceDraft] = Field(min_length=1, max_length=4)


class ReplaceFactToolOperation(StrictModel):
    operation: Literal[OperationType.REPLACE_FACT]
    target_fact_id: str
    new_fact: FactDraft


class RemoveFactToolOperation(StrictModel):
    operation: Literal[OperationType.REMOVE_FACT]
    target_fact_id: str


ToolProposalOperation = Annotated[
    AddFactToolOperation
    | AddActionPrerequisiteToolOperation
    | ReplaceFactToolOperation
    | RemoveFactToolOperation,
    Field(discriminator="operation"),
]


class UpdateDraft(StrictModel):
    target_id: str
    base_version: int = Field(ge=1)
    operations: list[ProposalOperation] = Field(min_length=1, max_length=12)
    reason: str = Field(min_length=1, max_length=500)


class AnswerCandidate(StrictModel):
    knowledge_id: str
    version: int = Field(ge=1)
    action_fact_id: str
    condition_fact_ids: list[str] = Field(max_length=12)
    evidence_segment_ids: list[str] = Field(min_length=1, max_length=6)
    supporting_fact_ids: list[str] = Field(default_factory=list, max_length=12)


class ClarificationRequest(StrictModel):
    question: str = Field(min_length=1, max_length=300)
    related_condition_id: str | None = None


class AnswerSelection(StrictModel):
    intent: Literal[
        "candidate_search", "reason_explanation", "evidence_lookup", "condition_comparison"
    ] = "candidate_search"
    status: Literal["candidates", "needs_clarification", "insufficient_evidence", "conflict"]
    candidates: list[AnswerCandidate] = Field(max_length=3)
    clarification_requests: list[ClarificationRequest] = Field(max_length=3)
    conflicting_fact_ids: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def validate_status(self) -> AnswerSelection:
        if self.status == "candidates" and not self.candidates:
            raise ValueError("candidate status requires at least one candidate")
        if self.status == "insufficient_evidence" and self.candidates:
            raise ValueError("insufficient evidence cannot contain candidates")
        return self


class ProposalSelection(StrictModel):
    proposal_id: str = Field(min_length=1, max_length=128)


class SessionRecord(StrictModel):
    id: str
    role: Role
    expires_at: datetime
    auth_version: str


class SearchHit(StrictModel):
    knowledge_id: str
    display_name: str
    title: str
    version: int
    equipment: str
    case_label: str | None
    source_kind: Literal["document", "interview", "mixed"]
    origin_label: str
    score: float
    fact_ids: list[str]
    evidence_segment_ids: list[str]
    condition_matches: dict[str, Literal["matched", "contradicted", "unknown"]]


class SearchSuccess(StrictModel):
    status: Literal["success"] = "success"
    hits: list[SearchHit]
    executed_at: datetime
    kb_revision: int


class ConsultationIntent(StrEnum):
    CANDIDATE_SEARCH = "candidate_search"
    REASON_EXPLANATION = "reason_explanation"
    EVIDENCE_LOOKUP = "evidence_lookup"
    CONDITION_COMPARISON = "condition_comparison"


class ConversationState(StrictModel):
    equipment: str | None = None
    actual_context: list[str] = Field(default_factory=list, max_length=6)
    hypothetical_context: list[str] = Field(default_factory=list, max_length=6)
    focus_answer_id: str | None = None
    focus_knowledge_ids: list[str] = Field(default_factory=list, max_length=3)
    last_intent: ConsultationIntent = ConsultationIntent.CANDIDATE_SEARCH
    reference_is_ambiguous: bool = False


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INDETERMINATE = "indeterminate"
    STALE_CONTEXT = "stale_context"


TERMINAL_JOB_STATES = {
    JobState.SUCCEEDED,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.EXPIRED,
    JobState.INDETERMINATE,
    JobState.STALE_CONTEXT,
}
