"""Deterministic consultation state separate from approved knowledge."""

from __future__ import annotations

import re
import unicodedata

from wg4_demo.repository import Repository
from wg4_demo.schemas import ConsultationIntent, ConversationState


class ConversationService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def prepare_turn(
        self,
        workspace_id: str,
        conversation_id: str,
        text: str,
        *,
        selected_knowledge_id: str | None = None,
    ) -> ConversationState:
        state = self.repository.get_conversation_state(workspace_id, conversation_id)
        normalized = unicodedata.normalize("NFKC", text)
        equipments = list(dict.fromkeys(re.findall(r"(?:冷却器|ポンプ|貯槽)\d+", normalized)))
        if len(equipments) == 1 and equipments[0] != state.equipment:
            state = state.model_copy(
                update={
                    "equipment": equipments[0],
                    "actual_context": [],
                    "hypothetical_context": [],
                    "focus_answer_id": None,
                    "focus_knowledge_ids": [],
                }
            )

        if "なぜ" in normalized or "理由" in normalized:
            intent = ConsultationIntent.REASON_EXPLANATION
        elif any(token in normalized for token in ("どの記録", "どこに", "原文", "出典")):
            intent = ConsultationIntent.EVIDENCE_LOOKUP
        elif any(token in normalized for token in ("場合", "だったら", "ならどう")):
            intent = ConsultationIntent.CONDITION_COMPARISON
        else:
            intent = ConsultationIntent.CANDIDATE_SEARCH

        actual = list(state.actual_context)
        hypothetical = list(state.hypothetical_context)
        is_correction = any(token in normalized for token in ("訂正", "実際には", "正しくは"))
        if is_correction:
            actual = [*actual, text][-6:]
            hypothetical = []
        elif intent is ConsultationIntent.CONDITION_COMPARISON:
            hypothetical = [*hypothetical, text][-6:]
        elif intent is ConsultationIntent.CANDIDATE_SEARCH:
            actual = [*actual, text][-6:]

        focus = list(state.focus_knowledge_ids)
        if selected_knowledge_id is not None:
            self.repository.get_knowledge(workspace_id, selected_knowledge_id)
            focus = [selected_knowledge_id]
        refers_to_previous = any(token in normalized for token in ("それ", "この確認", "その確認"))
        ambiguous = refers_to_previous and len(focus) != 1
        state = state.model_copy(
            update={
                "actual_context": actual,
                "hypothetical_context": hypothetical,
                "focus_knowledge_ids": focus,
                "last_intent": intent,
                "reference_is_ambiguous": ambiguous,
            }
        )
        self.repository.save_conversation_state(workspace_id, conversation_id, state)
        return state
