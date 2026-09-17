from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import streamlit as st

from wg4_demo.errors import ValidationFailure
from wg4_demo.schemas import AnswerSelection, FactKind
from wg4_demo.services import Services
from wg4_demo.ui.common import active_job_exists, enqueue, show_action_error


def render(services: Services, project_root: Path) -> None:
    st.header("2. エージェントに相談する")
    st.caption(
        "質問ごとに現行の承認版を検索し直します。会話中の発言は、承認なしに正式知識へ保存しません。"
    )
    demo = json.loads((project_root / "data" / "demo_inputs_v5.json").read_text("utf-8"))
    columns = st.columns(2)
    if columns[0].button("新しい会話（知識は残す）", disabled=active_job_exists(services)):
        st.session_state.conversation_id = services.repository.create_conversation(
            st.session_state.workspace_id
        )
        st.session_state.get("last_outcomes", {}).pop("qa", None)
        st.session_state.pop("consult_knowledge_id", None)
        st.success("質問履歴と相談条件だけを空にしました。承認済み知識は保持されています。")
        st.rerun()
    columns[1].caption(f"会話ID: {st.session_state.conversation_id[:8]}")

    _render_conversation_state(services)
    messages = services.repository.list_messages(
        st.session_state.workspace_id, st.session_state.conversation_id
    )
    if messages:
        with st.expander("この会話の履歴（直近12発言）"):
            for message in messages:
                st.text(f"{message['role']}: {message['text']}")

    default_question = st.session_state.pop(
        "consult_question", demo["free_dialogue_example"][0]["text"]
    )
    with st.form("qa-form"):
        question = st.text_area(
            "質問または追質問",
            value=default_question,
            max_chars=services.settings.max_input_chars,
            help="例：なぜですか／どの記録ですか／流量が低い場合も同じですか",
        )
        submitted = st.form_submit_button(
            "現行知識を調べて相談する（APIを使用）",
            disabled=active_job_exists(services),
        )
    if submitted:
        try:
            selected_id = st.session_state.get("consult_knowledge_id")
            state = services.conversations.prepare_turn(
                st.session_state.workspace_id,
                st.session_state.conversation_id,
                question,
                selected_knowledge_id=selected_id,
            )
            if state.reference_is_ambiguous:
                st.warning(
                    "「それ」が指す候補を一つに特定できません。検索結果から対象を選ぶか、"
                    "知識名・設備名を質問に含めてください。APIには送信していません。"
                )
            else:
                consultation = services.conversations.payload_for_turn(
                    state,
                    explicit_selected_knowledge_id=(
                        selected_id if isinstance(selected_id, str) else None
                    ),
                )
                services.repository.append_message(
                    st.session_state.workspace_id,
                    st.session_state.conversation_id,
                    role="user",
                    text=question,
                )
                enqueue(
                    services,
                    mode="qa",
                    payload={
                        "question": question,
                        "consultation": consultation,
                    },
                )
                st.session_state.pop("consult_knowledge_id", None)
                st.rerun()
        except Exception as exc:
            show_action_error(exc)
    outcome = st.session_state.get("last_outcomes", {}).get("qa")
    if outcome:
        render_answer(
            services, AnswerSelection.model_validate(outcome["selection"]), outcome["tools"]
        )


def _render_conversation_state(services: Services) -> None:
    try:
        state = services.repository.get_conversation_state(
            st.session_state.workspace_id, st.session_state.conversation_id
        )
    except Exception:
        return
    with st.expander("現在の相談条件と仮定を確認", expanded=False):
        st.write(f"設備：{state.equipment or '未指定'}")
        st.write("実際の状況：" + (" / ".join(state.actual_context) or "未指定"))
        st.write("仮定の別条件：" + (" / ".join(state.hypothetical_context) or "なし"))
        st.caption(f"直近の意図：{state.last_intent.value}")


def render_answer(
    services: Services,
    answer: AnswerSelection,
    tools: list[str],
    *,
    heading: str | None = None,
) -> None:
    prepared: list[tuple[Any, Any, dict[str, Any], list[dict[str, Any]], Any | None]] = []
    for candidate in answer.candidates:
        item = services.repository.get_knowledge(
            st.session_state.workspace_id, candidate.knowledge_id, candidate.version
        )
        facts = {fact.id: fact for fact in item.facts}
        action = facts.get(candidate.action_fact_id)
        if action is None or action.kind is not FactKind.CHECK_ACTION:
            raise ValidationFailure(
                "保存済み回答の確認行動が参照版と一致しません。",
                code="display_action_mismatch",
            )
        version_evidence_ids = {ref.segment_id for fact in item.facts for ref in fact.evidence_refs}
        if not set(candidate.evidence_segment_ids).issubset(version_evidence_ids):
            raise ValidationFailure(
                "保存済み回答の根拠IDが参照版と一致しません。",
                code="display_evidence_mismatch",
            )
        evidence = services.repository.read_segments(
            st.session_state.workspace_id, candidate.evidence_segment_ids
        )
        try:
            graph = services.graph.get_context(st.session_state.workspace_id, item.id, item.version)
        except Exception:
            graph = None
        prepared.append((candidate, item, facts, evidence, graph))

    if heading:
        st.subheader(heading)
    st.caption(f"相談意図: {answer.intent}")
    if answer.status == "insufficient_evidence":
        st.warning("検索は正常に完了しましたが、承認済み根拠は見つかりませんでした。")
    elif answer.status == "needs_clarification":
        st.info("対象または条件の追加確認が必要です。")
        for request in answer.clarification_requests:
            st.write(request.question)
    elif answer.status == "conflict":
        st.warning("同じ文脈に矛盾する承認済みfactがあります。勝手に解消していません。")
    for candidate, item, facts, evidence, graph in prepared:
        action = facts[candidate.action_fact_id]
        with st.container(border=True):
            st.subheader(f"確認候補：{action.text}")
            st.caption(
                f"参照: {item.display_name} v{item.version} — {item.title} / "
                f"由来: {item.source_kind} / 原因: {item.cause_status.value}"
            )
            st.write("適用条件・行動前提")
            if candidate.condition_fact_ids:
                for fact_id in candidate.condition_fact_ids:
                    fact = facts[fact_id]
                    scope = fact.condition_scope.value if fact.condition_scope else "未分類"
                    st.write(f"- {fact.text}（{scope}）")
            else:
                st.write("- 明示された条件なし")
            has_decision_reason = any(
                facts[fact_id].kind is FactKind.DECISION_REASON
                for fact_id in candidate.supporting_fact_ids
            )
            if candidate.supporting_fact_ids:
                st.write("判断理由・例外・補足")
                for fact_id in candidate.supporting_fact_ids:
                    fact = facts[fact_id]
                    label = "判断理由" if fact.kind is FactKind.DECISION_REASON else fact.kind.value
                    st.write(f"- {label}: {fact.text}")
            if answer.intent == "reason_explanation" and not has_decision_reason:
                st.warning("この知識には、本人が述べた判断理由の記録がありません。")
            if item.missing_fields:
                st.write("未確認事項: " + "、".join(item.missing_fields))
            with st.expander("原文の根拠", expanded=True):
                for segment in evidence:
                    st.caption(f"{segment['title']} / {segment['kind']} / 原文{segment['ordinal']}")
                    st.text(str(segment["text"]))
            if graph is not None:
                with st.expander("関連する知識グラフ"):
                    st.graphviz_chart(graph.dot)
            else:
                st.warning("関係図だけを表示できません。候補と原文は確定済みです。")
    st.caption("実行したツール: " + " → ".join(tools))


def answer_fact_ids(payload: dict[str, Any]) -> set[str]:
    """Return exact fact ids used by a persisted answer snapshot."""

    answer = AnswerSelection.model_validate(payload["selection"])
    return {
        fact_id
        for candidate in answer.candidates
        for fact_id in (
            candidate.action_fact_id,
            *candidate.condition_fact_ids,
            *candidate.supporting_fact_ids,
        )
    }
