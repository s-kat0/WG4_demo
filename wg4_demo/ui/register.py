from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st

from wg4_demo.services import Services
from wg4_demo.ui.common import active_job_exists, enqueue, show_action_error


def render(services: Services, project_root: Path) -> None:
    st.header("1. 文書・経験を登録")
    maintenance = json.loads((project_root / "data" / "maintenance1.json").read_text("utf-8"))
    interview = json.loads((project_root / "data" / "interview1.json").read_text("utf-8"))
    sample_text = "\n".join(segment["text"] for segment in maintenance["segments"])
    with st.form("extract-form"):
        document_text = st.text_area(
            "保全記録（架空データのみ）",
            value=sample_text,
            height=150,
            max_chars=services.settings.max_document_chars,
        )
        extract = st.form_submit_button(
            "文書から知識候補を抽出", disabled=active_job_exists(services)
        )
    if extract:
        try:
            lines = [line.strip() for line in document_text.splitlines() if line.strip()]
            _, mapping = services.repository.register_source(
                st.session_state.workspace_id,
                title="保全記録1",
                kind="document",
                equipment="冷却器1",
                case_label="事例1",
                external_key=f"maintenance1-{uuid4()}",
                segments=[
                    (f"maintenance1-{uuid4()}-p{i}", "document", text)
                    for i, text in enumerate(lines, 1)
                ],
            )
            segments = [
                {"segment_id": segment_id, "text": text, "speaker": "document"}
                for segment_id, text in zip(mapping.values(), lines, strict=True)
            ]
            st.session_state.case_segments = segments
            enqueue(services, mode="extract", payload={"segments": segments})
            st.rerun()
        except Exception as exc:
            show_action_error(exc)
    outcomes = st.session_state.get("last_outcomes", {})
    extracted = outcomes.get("extract")
    if extracted:
        st.subheader("抽出された未承認カード")
        _show_draft(extracted["draft"])
    with st.form("interview-form"):
        opening = st.text_area("経験談", value=interview["opening"], max_chars=1500)
        ask = st.form_submit_button(
            "不足情報を一つ質問",
            disabled=active_job_exists(services) or extracted is None,
        )
    if ask and extracted:
        try:
            _, mapping = services.repository.register_source(
                st.session_state.workspace_id,
                title="聞き取り記録1（開始発言）",
                kind="interview",
                equipment="冷却器1",
                case_label="事例1",
                external_key=f"interview-opening-{uuid4()}",
                segments=[(f"opening-{uuid4()}", "operator", opening)],
            )
            opening_segment = {
                "segment_id": next(iter(mapping.values())),
                "text": opening,
                "speaker": "operator",
            }
            st.session_state.opening_segment = opening_segment
            enqueue(
                services,
                mode="interview",
                payload={
                    "draft": extracted["draft"],
                    "statements": [opening_segment],
                },
            )
            st.rerun()
        except Exception as exc:
            show_action_error(exc)
    interview_outcome = outcomes.get("interview")
    if interview_outcome:
        st.info(f"LLMの追加質問：{interview_outcome['question']}")
    with st.form("reflect-form"):
        answer = st.text_area("回答", value=interview["prepared_answer"], max_chars=1500)
        reflect = st.form_submit_button(
            "回答を反映して承認候補を作る",
            disabled=active_job_exists(services) or interview_outcome is None,
        )
    if reflect and interview_outcome:
        try:
            _, mapping = services.repository.register_source(
                st.session_state.workspace_id,
                title="聞き取り記録1（質問と回答）",
                kind="interview",
                equipment="冷却器1",
                case_label="事例1",
                external_key=f"interview-answer-{uuid4()}",
                segments=[
                    (f"question-{uuid4()}", "assistant", interview_outcome["question"]),
                    (f"answer-{uuid4()}", "operator", answer),
                ],
            )
            ids = list(mapping.values())
            new_segments = [
                {
                    "segment_id": ids[0],
                    "text": interview_outcome["question"],
                    "speaker": "assistant",
                },
                {"segment_id": ids[1], "text": answer, "speaker": "operator"},
            ]
            all_segments = [
                *st.session_state.get("case_segments", []),
                st.session_state.opening_segment,
                *new_segments,
            ]
            allowed_ids = [
                segment["segment_id"]
                for segment in all_segments
                if segment["speaker"] != "assistant"
            ]
            enqueue(
                services,
                mode="reflect",
                payload={
                    "segments": all_segments,
                    "allowed_segment_ids": allowed_ids,
                    "equipment": "冷却器1",
                    "case_label": "事例1",
                },
            )
            st.rerun()
        except Exception as exc:
            show_action_error(exc)
    proposal_outcome = outcomes.get("reflect")
    if proposal_outcome:
        st.success("更新案をpendingとして保存しました。「更新案を確認」で人が承認できます。")


def _show_draft(draft: dict[str, Any]) -> None:
    for fact in draft.get("facts", []):
        st.write(f"- {fact['kind']}: {fact['text']}")
    st.caption("原因の確定状況: " + str(draft.get("cause_status")))
    st.caption("未確認: " + "、".join(draft.get("missing_fields", [])))
