from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from wg4_demo.schemas import AnswerSelection
from wg4_demo.services import Services
from wg4_demo.ui.common import active_job_exists, enqueue, show_action_error


def render(services: Services, project_root: Path) -> None:
    st.header("3. 質問して使う")
    demo = json.loads((project_root / "data" / "demo_inputs.json").read_text("utf-8"))
    cols = st.columns(2)
    if cols[0].button("新しい会話", disabled=active_job_exists(services)):
        st.session_state.conversation_id = services.repository.create_conversation(
            st.session_state.workspace_id
        )
        st.session_state.get("last_outcomes", {}).pop("qa", None)
        st.success("質問履歴だけを空にしました。承認済み知識は保持されています。")
    cols[1].caption(f"会話ID: {st.session_state.conversation_id[:8]}")
    messages = services.repository.list_messages(
        st.session_state.workspace_id, st.session_state.conversation_id
    )
    if messages:
        with st.expander("この会話の履歴（直近12発言）"):
            for message in messages:
                st.text(f"{message['role']}: {message['text']}")
    with st.form("qa-form"):
        question = st.text_area(
            "質問",
            value=demo["main_question"],
            max_chars=services.settings.max_input_chars,
        )
        submitted = st.form_submit_button(
            "根拠付き候補を探す", disabled=active_job_exists(services)
        )
    if submitted:
        try:
            services.repository.append_message(
                st.session_state.workspace_id,
                st.session_state.conversation_id,
                role="user",
                text=question,
            )
            enqueue(services, mode="qa", payload={"question": question})
            st.rerun()
        except Exception as exc:
            show_action_error(exc)
    outcome = st.session_state.get("last_outcomes", {}).get("qa")
    if outcome:
        _render_answer(
            services, AnswerSelection.model_validate(outcome["selection"]), outcome["tools"]
        )


def _render_answer(services: Services, answer: AnswerSelection, tools: list[str]) -> None:
    if answer.status == "insufficient_evidence":
        st.warning("検索は正常に完了しましたが、承認済み根拠は見つかりませんでした。")
    elif answer.status == "needs_clarification":
        st.info("追加確認が必要です。")
        for request in answer.clarification_requests:
            st.write(request.question)
    elif answer.status == "conflict":
        st.warning("同じ文脈に矛盾する承認済みfactがあります。勝手に解消していません。")
    for candidate in answer.candidates:
        item = services.repository.get_knowledge(
            st.session_state.workspace_id, candidate.knowledge_id, candidate.version
        )
        facts = {fact.id: fact for fact in item.facts}
        action = facts[candidate.action_fact_id]
        st.subheader(f"確認候補：{action.text}")
        st.caption(f"参照: {item.display_name} v{item.version} / 原因: {item.cause_status.value}")
        st.write("適用条件・行動前提")
        for fact_id in candidate.condition_fact_ids:
            st.write(f"- {facts[fact_id].text}")
        if item.missing_fields:
            st.write("未確認事項: " + "、".join(item.missing_fields))
        evidence = services.evidence.read_for_qa(
            st.session_state.workspace_id, candidate.evidence_segment_ids
        )
        with st.expander("原文の根拠"):
            for segment in evidence:
                st.text(f"{segment['title']}: {segment['text']}")
        try:
            graph = services.graph.get_context(st.session_state.workspace_id, item.id, item.version)
            st.graphviz_chart(graph.dot)
        except Exception:
            st.warning("関係図だけを表示できません。候補と原文は確定済みです。")
    st.caption("実行したツール: " + " → ".join(tools))
