from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import streamlit as st

from wg4_demo.repository import KnowledgeRecord, ProposalRecord
from wg4_demo.services import Services
from wg4_demo.ui.common import active_job_exists, enqueue, show_action_error

KIND_LABELS = {
    "observation": "観察・症状",
    "check_action": "実施した確認行動",
    "condition": "条件",
    "cause_hypothesis": "原因仮説",
    "decision_reason": "判断理由",
    "exception": "例外",
    "cause_status": "原因の確定状況",
}

OPERATION_LABELS = {
    "add_fact": "追加",
    "replace_fact": "置換",
    "remove_fact": "削除",
}

SCOPE_LABELS = {
    "case_context": "事例条件",
    "action_prerequisite": "行動前提",
    "exclusion": "除外条件",
}


def render(services: Services, project_root: Path) -> None:
    st.header("4. 更新案を作成・確認")
    demo = json.loads((project_root / "data" / "demo_inputs.json").read_text("utf-8"))
    try:
        items = services.repository.list_knowledge(st.session_state.workspace_id)
        pending = services.repository.list_pending_proposals(st.session_state.workspace_id)
        item3 = next((item for item in items if item.display_name == "知識項目3"), None)
        qa_completed = "qa" in st.session_state.get("last_outcomes", {})
        _render_current_step(pending, item3, qa_completed)
        _render_pending(services, pending)
        _render_update_form(
            services,
            demo["update_statement"],
            item3,
            qa_completed=qa_completed,
            has_pending=bool(pending),
        )
    except Exception as exc:
        show_action_error(exc)


def _render_current_step(
    pending: list[ProposalRecord], item3: KnowledgeRecord | None, qa_completed: bool
) -> None:
    with st.expander("この画面で行うこと", expanded=True):
        if pending:
            st.markdown(
                "**今やること：下の「人の確認待ち」を確認します。**  "
                "内容と原文根拠が正しければ確認欄にチェックして承認し、"
                "誤り・推測・根拠不足があれば却下します。更新作成欄はまだ使いません。"
            )
        elif item3 is None:
            st.markdown(
                "**次に選ぶ画面：サイドバーの「文書・経験を登録」**  "
                "抽出、追加質問、回答反映まで実行すると、この画面にpending案が現れます。"
            )
        elif item3.version == 1 and not qa_completed:
            st.markdown(
                "**次に選ぶ画面：サイドバーの「質問して使う」**  "
                "v1で主質問を実行した後、この画面へ戻って新しい発言を登録します。"
            )
        elif item3.version == 1:
            st.markdown(
                "**今やること：下の「新しい発言からv2更新案を作る」を開きます。**  "
                "更新対象は知識項目3 v1に固定されています。例文を確認して実行してください。"
            )
        else:
            st.markdown(
                "**次に選ぶ画面：サイドバーの「質問して使う」**  "
                "「新しい会話」を押してから同じ主質問を実行し、v2と追加根拠を確認します。"
            )


def _render_pending(services: Services, pending: list[ProposalRecord]) -> None:
    st.subheader("いま行う：人の確認待ち")
    if not pending:
        st.caption("現在、確認待ちの案はありません。上の案内に従って次の画面を選びます。")
        return
    for proposal in pending:
        with st.container(border=True):
            destination = (
                "新規知識（承認するとv1）"
                if proposal.base_version == 0
                else f"既存知識 v{proposal.base_version} → v{proposal.base_version + 1}"
            )
            st.markdown(f"**承認対象：{destination}**")
            st.write(f"作成理由：{proposal.reason}")
            st.caption(f"内容識別子: {proposal.content_hash[:12]} / 状態: pending")
            with st.expander("合格基準を見る", expanded=True):
                if proposal.base_version == 0:
                    st.markdown(
                        "- 症状、温度計交換後、別計器との照合、冷却水側の条件、"
                        "原因未特定が原文どおりに整理されている\n"
                        "- 原文にない故障原因、照合結果、一般化を追加していない\n"
                        "- **温度計交換後は「条件」**として独立している"
                    )
                else:
                    st.markdown(
                        "- 照合用計器の校正確認が、確認行動の**行動前提**として追加される\n"
                        "- 「校正が有効だった」という観測事実へ変えていない\n"
                        "- 承認前の通常検索結果は旧版のまま"
                    )
            for operation in proposal.operations:
                operation_label = OPERATION_LABELS[operation.operation.value]
                if operation.new_fact:
                    fact = operation.new_fact
                    kind_label = KIND_LABELS[fact.kind.value]
                    scope = (
                        f" / {SCOPE_LABELS[fact.condition_scope.value]}"
                        if fact.condition_scope
                        else ""
                    )
                    st.markdown(f"**{operation_label}・{kind_label}{scope}**：{fact.text}")
                    for evidence in fact.evidence:
                        st.caption(f"引用：『{evidence.quote}』")
                else:
                    st.write(f"{operation_label}：既存fact {operation.target_fact_id}")
            if proposal.missing_fields:
                st.warning("未確認事項：" + "、".join(proposal.missing_fields))
            st.caption(f"原因の確定状況：{proposal.cause_status.value}")
            evidence_ids = [
                evidence.segment_id
                for operation in proposal.operations
                if operation.new_fact
                for evidence in operation.new_fact.evidence
            ]
            if evidence_ids:
                segments = services.repository.read_segments(
                    st.session_state.workspace_id, evidence_ids
                )
                unique_segments = {segment["id"]: segment for segment in segments}
                with st.expander("原文の根拠を確認", expanded=True):
                    for segment in unique_segments.values():
                        st.caption(f"{segment['title']} / 原文{segment['ordinal']}")
                        st.text(segment["text"])
            confirmed = st.checkbox(
                "内容、factの種類、未確認事項、原文根拠を確認しました",
                key=f"confirmed-{proposal.id}",
            )
            approve, reject = st.columns(2)
            if approve.button(
                f"確認してv{proposal.base_version + 1}として承認",
                key=f"approve-{proposal.id}",
                disabled=active_job_exists(services) or not confirmed,
            ):
                result = services.approvals.approve(
                    session_id=st.session_state.session_id,
                    workspace_id=st.session_state.workspace_id,
                    proposal_id=proposal.id,
                    expected_content_hash=proposal.content_hash,
                )
                st.success(f"v{result.after_version} として承認しました。")
                st.rerun()
            if reject.button(
                "内容に問題があるため却下",
                key=f"reject-{proposal.id}",
                disabled=active_job_exists(services),
            ):
                services.approvals.reject(
                    session_id=st.session_state.session_id,
                    workspace_id=st.session_state.workspace_id,
                    proposal_id=proposal.id,
                )
                st.rerun()


def _render_update_form(
    services: Services,
    update_statement: str,
    item3: KnowledgeRecord | None,
    *,
    qa_completed: bool,
    has_pending: bool,
) -> None:
    ready = item3 is not None and item3.version == 1 and qa_completed and not has_pending
    with st.expander("次の工程：新しい発言からv2更新案を作る", expanded=ready):
        if has_pending:
            st.info("先に上のpending案を承認または却下してください。この欄はまだ使いません。")
        elif item3 is None:
            st.info("先に「文書・経験を登録」で最初の承認候補を作ってください。")
        elif item3.version != 1:
            st.success("知識項目3はすでにv2です。新しい会話で再検索してください。")
        elif not qa_completed:
            st.info("先に「質問して使う」でv1の根拠付き回答を確認してください。")
        with st.form("update-form"):
            target_label = f"{item3.display_name} v{item3.version}" if item3 else "未作成"
            st.text_input(
                "更新対象（このデモでは知識項目3）",
                value=target_label,
                disabled=True,
            )
            statement = st.text_area(
                "新しい発言",
                value=update_statement,
                max_chars=services.settings.max_input_chars,
                disabled=not ready,
            )
            submit = st.form_submit_button(
                "この発言から更新案を作る",
                disabled=active_job_exists(services) or not ready,
            )
        if submit and item3:
            _, mapping = services.repository.register_source(
                st.session_state.workspace_id,
                title="聞き取り記録2",
                kind="interview",
                equipment=item3.equipment,
                case_label=item3.case_label,
                external_key=f"update-{uuid4()}",
                segments=[(f"update-p1-{uuid4()}", "operator", statement)],
            )
            segment_id = next(iter(mapping.values()))
            enqueue(
                services,
                mode="update",
                payload={
                    "statement": statement,
                    "submitted_segment_ids": [segment_id],
                    "target_knowledge_id": item3.id,
                    "target_version": item3.version,
                    "equipment": item3.equipment,
                },
            )
            st.rerun()
