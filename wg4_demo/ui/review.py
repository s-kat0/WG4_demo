from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st

from wg4_demo.repository import AnswerSnapshotRecord, KnowledgeRecord, ProposalRecord
from wg4_demo.schemas import AnswerSelection
from wg4_demo.services import Services
from wg4_demo.ui.common import active_job_exists, enqueue, show_action_error
from wg4_demo.ui.qa import answer_fact_ids, render_answer
from wg4_demo.ui.register import _enqueue_comparison

KIND_LABELS = {
    "observation": "観察・症状",
    "check_action": "実施した確認行動",
    "condition": "条件",
    "cause_hypothesis": "原因仮説",
    "decision_reason": "判断理由",
    "exception": "例外",
    "cause_status": "原因の確定状況",
}

SCOPE_LABELS = {
    "case_context": "当該事例の条件",
    "applicability": "参照してよい条件",
    "action_prerequisite": "確認行動の前提",
    "exclusion": "除外条件",
}


def render(services: Services, project_root: Path) -> None:
    st.header("4. 更新案・実回答比較")
    demo = json.loads((project_root / "data" / "demo_inputs_v5.json").read_text("utf-8"))
    try:
        workspace = services.repository.require_workspace(
            st.session_state.session_id, st.session_state.workspace_id
        )
        item = (
            services.repository.get_knowledge(workspace.id, workspace.lecture_case_item_id)
            if workspace.lecture_case_item_id
            else None
        )
        pending = services.repository.list_pending_proposals(workspace.id)
        _render_guide(pending, item)
        _render_pending(services, pending)
        snapshots = services.repository.list_answer_snapshots(workspace.id)
        _render_comparison_controls(services, demo, item, snapshots, bool(pending))
        _render_snapshots(services, snapshots)
        _render_feedback(services, demo, item, snapshots, bool(pending))
    except Exception as exc:
        show_action_error(exc)


def _render_guide(pending: list[ProposalRecord], item: KnowledgeRecord | None) -> None:
    with st.expander("この画面で今すること", expanded=True):
        if pending:
            st.markdown(
                "**下の『人の確認待ち』を確認してください。**  "
                "追加内容と原文が一致し、推測がなければ確認欄を選んで承認します。"
            )
        elif item is None:
            st.markdown(
                "**次は『知識を追加・補足する』です。** 文書を抽出し、文書だけの承認候補を作ります。"
            )
        elif item.version == 1:
            st.markdown(
                "**文書版v1です。** 『知識を追加・補足する』で回答Aと本人役への聞き取りを進めます。"
            )
        elif item.version == 2:
            st.markdown(
                "**対話補足版v2です。** 下で回答Bを実行し、その実候補へ校正条件を補足します。"
            )
        else:
            st.markdown(
                "**フィードバック承認後の版です。** 下で空履歴の回答Cを実行し、B/Cの根拠差を確認します。"
            )


def _render_pending(services: Services, pending: list[ProposalRecord]) -> None:
    st.subheader("人の確認待ち")
    if not pending:
        st.caption("現在、確認待ちの案はありません。")
        return
    for proposal in pending:
        with st.container(border=True):
            destination = (
                "新規知識（承認するとv1）"
                if proposal.base_version == 0
                else f"現行v{proposal.base_version} → 新版v{proposal.base_version + 1}"
            )
            st.markdown(f"**承認対象：{destination}**")
            if proposal.title:
                st.write(f"知識名：{proposal.title}")
            st.write(f"作成理由：{proposal.reason}")
            st.caption(f"内容識別子: {proposal.content_hash[:12]} / 状態: pending")
            for operation in proposal.operations:
                if operation.new_fact:
                    fact = operation.new_fact
                    scope = (
                        f" / {SCOPE_LABELS[fact.condition_scope.value]}"
                        if fact.condition_scope
                        else ""
                    )
                    st.markdown(f"**追加・{KIND_LABELS[fact.kind.value]}{scope}**：{fact.text}")
                    for evidence in fact.evidence:
                        st.caption(f"引用：『{evidence.quote}』")
                else:
                    st.write(f"{operation.operation.value}：既存fact {operation.target_fact_id}")
            if proposal.missing_fields:
                st.warning("承認後も残る未確認事項：" + "、".join(proposal.missing_fields))
            st.caption(f"原因の確定状況：{proposal.cause_status.value}")
            evidence_ids = list(
                dict.fromkeys(
                    evidence.segment_id
                    for operation in proposal.operations
                    if operation.new_fact
                    for evidence in operation.new_fact.evidence
                )
            )
            if evidence_ids:
                with st.expander("原文と差分を照合", expanded=True):
                    for segment in services.repository.read_segments(
                        st.session_state.workspace_id, evidence_ids
                    ):
                        st.caption(f"{segment['title']} / {segment['speaker']}")
                        st.text(str(segment["text"]))
            confirmed = st.checkbox(
                "追加内容、fact種別、確定度、未確認事項、原文を照合しました",
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
                st.success(f"{result.item_id[:8]} をv{result.after_version}として承認しました。")
                st.session_state.pop("search_result", None)
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


def _render_comparison_controls(
    services: Services,
    demo: dict[str, Any],
    item: KnowledgeRecord | None,
    snapshots: list[AnswerSnapshotRecord],
    has_pending: bool,
) -> None:
    if item is None:
        return
    by_stage = {snapshot.stage: snapshot for snapshot in snapshots}
    expected_stage = "A" if item.version == 1 else "B" if item.version == 2 else "C"
    existing = by_stage.get(expected_stage)
    if existing is not None and existing.state == "succeeded":
        return
    st.subheader(f"比較回答{expected_stage}を実行")
    st.write(demo["comparison_question"])
    st.caption("同じ質問・モデル・prompt・検索設定を使い、毎回新しい空のQA会話で実行します。")
    if existing is not None:
        st.warning(
            f"前回の回答{expected_stage}は{existing.safe_error_code or 'unknown_error'}で失敗しました。"
            "下のボタンは自動再送ではなく、利用者が開始する新しい操作です。"
        )
    button_label = (
        f"新しい操作として回答{expected_stage}を再実行（APIを使用）"
        if existing is not None
        else f"空の会話で回答{expected_stage}を実行（APIを使用）"
    )
    if st.button(
        button_label,
        disabled=active_job_exists(services) or has_pending,
        key=f"run-comparison-{expected_stage}",
    ):
        _enqueue_comparison(services, demo["comparison_question"], item, expected_stage)
        st.rerun()


def _render_snapshots(services: Services, snapshots: list[AnswerSnapshotRecord]) -> None:
    st.subheader("保存済みの実回答 A / B / C")
    if not snapshots:
        st.info("比較用の実行記録なし")
        return
    by_stage = {snapshot.stage: snapshot for snapshot in snapshots}
    for stage in ("A", "B", "C"):
        snapshot = by_stage.get(stage)
        if snapshot is None:
            st.caption(f"回答{stage}：比較用の実行記録なし")
            continue
        with st.expander(
            f"回答{stage} — {snapshot.state} / 対象v{snapshot.target_version}",
            expanded=stage in {"A", "B"},
        ):
            st.caption(
                f"実行 {snapshot.created_at} / KB改訂 {snapshot.kb_revision} / "
                f"model {snapshot.model_id} / prompt {snapshot.prompt_version} / 空履歴 {snapshot.empty_history}"
            )
            if snapshot.state == "failed" or snapshot.payload is None:
                st.error(
                    f"この実行は失敗しました：{snapshot.safe_error_code or 'unknown_error'}。"
                    "模範回答や過去回答では置き換えていません。"
                )
            else:
                render_answer(
                    services,
                    AnswerSelection.model_validate(snapshot.payload["selection"]),
                    list(snapshot.payload["tools"]),
                )
    if "A" in by_stage and "B" in by_stage:
        _render_pair_comparison("A", by_stage["A"], "B", by_stage["B"])
    if "B" in by_stage and "C" in by_stage:
        _render_pair_comparison("B", by_stage["B"], "C", by_stage["C"])


def _render_pair_comparison(
    left_label: str,
    left: AnswerSnapshotRecord,
    right_label: str,
    right: AnswerSnapshotRecord,
) -> None:
    same_settings = all(
        (
            left.question_hash == right.question_hash,
            left.model_id == right.model_id,
            left.model_settings == right.model_settings,
            left.prompt_version == right.prompt_version,
            left.schema_version == right.schema_version,
            left.retrieval_version == right.retrieval_version,
            left.empty_history and right.empty_history,
        )
    )
    if same_settings:
        st.success(f"{left_label}/{right_label}は同一条件の実回答比較です。")
    else:
        st.warning(f"{left_label}/{right_label}は設定差があるため参考比較です。")
    if left.payload and right.payload:
        left_ids = answer_fact_ids(left.payload)
        right_ids = answer_fact_ids(right.payload)
        st.write(f"{right_label}で新たに参照：{len(right_ids - left_ids)} fact")
        st.write(f"{right_label}で参照しなくなった：{len(left_ids - right_ids)} fact")


def _render_feedback(
    services: Services,
    demo: dict[str, Any],
    item: KnowledgeRecord | None,
    snapshots: list[AnswerSnapshotRecord],
    has_pending: bool,
) -> None:
    if item is None or item.version != 2 or has_pending:
        return
    stage_b = next(
        (
            snapshot
            for snapshot in snapshots
            if snapshot.stage == "B" and snapshot.state == "succeeded" and snapshot.payload
        ),
        None,
    )
    if stage_b is None:
        return
    stage_b_payload = stage_b.payload
    if stage_b_payload is None:
        return
    answer = AnswerSelection.model_validate(stage_b_payload["selection"])
    if not answer.candidates:
        st.warning("回答Bに実候補がないため、候補へのフィードバックは作成できません。")
        return
    candidate = next(
        (candidate for candidate in answer.candidates if candidate.knowledge_id == item.id),
        None,
    )
    if candidate is None:
        st.warning("回答Bが講演対象事例を候補に含めなかったため、その結果から更新案を作りません。")
        return
    target = services.repository.get_knowledge(
        st.session_state.workspace_id, candidate.knowledge_id
    )
    if target.version != candidate.version:
        st.warning("回答Bの参照版と現行版が異なるため、この画面から更新案を作りません。")
        return
    st.subheader("回答Bの実候補へ条件を一つ補足")
    st.caption(f"対象：{target.display_name} v{target.version} — {target.title}")
    with st.form("feedback-form"):
        statement = st.text_area(
            "本人役の新しい発言",
            value=demo["feedback"]["text"],
            max_chars=services.settings.max_input_chars,
        )
        submit = st.form_submit_button(
            "根拠・条件の更新案を作る（APIを使用）",
            disabled=active_job_exists(services),
        )
    if submit:
        try:
            _, mapping = services.repository.register_source(
                st.session_state.workspace_id,
                title=demo["feedback"]["title"],
                kind="interview",
                equipment=target.equipment,
                case_label=target.case_label,
                external_key=f"feedback-{uuid4()}",
                segments=[(f"feedback-p1-{uuid4()}", "operator", statement)],
            )
            segment_id = next(iter(mapping.values()))
            enqueue(
                services,
                mode="update",
                payload={
                    "statement": statement,
                    "submitted_segment_ids": [segment_id],
                    "target_knowledge_id": target.id,
                    "target_version": target.version,
                    "equipment": target.equipment,
                    "answer_action_id": stage_b.action_id,
                },
            )
            st.rerun()
        except Exception as exc:
            show_action_error(exc)
