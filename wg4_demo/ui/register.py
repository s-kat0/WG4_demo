from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st

from wg4_demo.errors import ValidationFailure
from wg4_demo.repository import KnowledgeRecord
from wg4_demo.schemas import KnowledgeDraft, OperationType, ProposalOperation
from wg4_demo.services import Services
from wg4_demo.ui.common import active_job_exists, enqueue, show_action_error


def render(services: Services, project_root: Path) -> None:
    st.header("3. 知識を追加・補足する")
    st.caption(
        "主実演は一つの冷却器1事例です。文書版を承認してAを記録し、本人役の理由・適用範囲を補います。"
    )
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
        with st.expander("この画面の進め方", expanded=True):
            st.markdown(
                "1. 文書を抽出し、文書だけの承認候補を作る\n"
                "2. 「更新案・実回答比較」で文書版v1を承認する\n"
                "3. この画面で比較回答Aを記録する\n"
                "4. 本人役へ理由と適用範囲を聞き、補足案を作る\n"
                "5. 「更新案・実回答比較」で新版を承認する"
            )
        _render_document_step(services, demo, item)
        if item is not None:
            _render_comparison_a(services, demo, item)
            _render_interview_step(services, demo, item)
    except Exception as exc:
        show_action_error(exc)


def _render_document_step(
    services: Services, demo: dict[str, Any], item: KnowledgeRecord | None
) -> None:
    st.subheader("A. 文書だけの知識を作る")
    if item is not None:
        st.success(f"対象事例は {item.display_name} v{item.version} として承認済みです。")
        return
    with st.form("extract-form"):
        document_text = st.text_area(
            "保全記録1（架空データ）",
            value=demo["document"]["text"],
            height=140,
            max_chars=services.settings.max_document_chars,
        )
        extract = st.form_submit_button(
            "文書から知識候補を抽出（APIを使用）",
            disabled=active_job_exists(services),
        )
    if extract:
        try:
            lines = [line.strip() for line in document_text.splitlines() if line.strip()]
            _, mapping = services.repository.register_source(
                st.session_state.workspace_id,
                title=demo["document"]["title"],
                kind="document",
                equipment=demo["document"]["equipment"],
                case_label=demo["document"]["case"],
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
    extracted = st.session_state.get("last_outcomes", {}).get("extract")
    if not extracted:
        return
    draft = KnowledgeDraft.model_validate_json(json.dumps(extracted["draft"], ensure_ascii=False))
    st.markdown("**抽出された未承認カード（原文を短く構造化）**")
    for fact in draft.facts:
        scope = f" / {fact.condition_scope.value}" if fact.condition_scope else ""
        st.write(f"- {fact.kind.value}{scope}: {fact.text}")
    st.caption("原因の確定状況: " + draft.cause_status.value)
    st.caption("未確認: " + ("、".join(draft.missing_fields) or "なし"))
    if st.button(
        "文書だけで承認候補を作る（API不使用）",
        disabled=active_job_exists(services),
    ):
        try:
            extraction_action_id = st.session_state.get("last_outcome_action_ids", {}).get(
                "extract"
            )
            if not isinstance(extraction_action_id, str):
                raise ValidationFailure(
                    "文書抽出の操作IDを確認できないため更新案を作成しません。",
                    code="extraction_action_missing",
                )
            segments = st.session_state.get("case_segments", [])
            allowed_ids = {str(segment["segment_id"]) for segment in segments}
            proposal = services.repository.stage_proposal(
                st.session_state.workspace_id,
                action_id=f"document-draft-{extraction_action_id}",
                target_item_id=None,
                base_version=0,
                operations=[
                    ProposalOperation(operation=OperationType.ADD_FACT, new_fact=fact)
                    for fact in draft.facts
                ],
                reason="文書だけから抽出した対象事例。判断理由と適用範囲は未確認",
                equipment=demo["document"]["equipment"],
                case_label=demo["document"]["case"],
                title="温度計交換後の出口温度表示",
                missing_fields=draft.missing_fields,
                cause_status=draft.cause_status,
                allowed_segment_ids=allowed_ids,
            )
            if proposal.status == "staged":
                services.repository.publish_proposal(st.session_state.workspace_id, proposal.id)
            elif proposal.status != "pending":
                raise ValidationFailure(
                    "この文書抽出の更新案は既に処理済みです。",
                    code="document_proposal_finalized",
                )
            st.session_state.nav_page = "更新案・実回答比較"
            st.rerun()
        except Exception as exc:
            show_action_error(exc)


def _render_comparison_a(services: Services, demo: dict[str, Any], item: KnowledgeRecord) -> None:
    st.subheader("B. 文書版の実回答Aを記録")
    snapshots = services.repository.list_answer_snapshots(st.session_state.workspace_id)
    stage_a = next((snapshot for snapshot in snapshots if snapshot.stage == "A"), None)
    if stage_a:
        if stage_a.state == "succeeded":
            st.success(f"回答Aを記録済み：{item.display_name} v{stage_a.target_version}")
        else:
            st.error(f"回答Aは失敗として記録済み：{stage_a.safe_error_code}")
        return
    if item.version != 1:
        st.info("回答Aは文書だけのv1で記録します。")
        return
    st.write(demo["comparison_question"])
    if st.button(
        "空の会話で比較回答Aを実行（APIを使用）",
        disabled=active_job_exists(services),
    ):
        _enqueue_comparison(services, demo["comparison_question"], item, "A")
        st.rerun()


def _render_interview_step(services: Services, demo: dict[str, Any], item: KnowledgeRecord) -> None:
    st.subheader("C. 本人役の理由・適用範囲を補う")
    if item.version >= 2:
        st.success(f"聞き取り補足は {item.display_name} v{item.version} に反映済みです。")
        return
    snapshots = services.repository.list_answer_snapshots(st.session_state.workspace_id)
    if not any(snapshot.stage == "A" for snapshot in snapshots):
        st.info("先に比較回答Aを実行し、文書版の実回答を保存してください。")
        return
    statements = st.session_state.setdefault("interview_statements", [])
    if not statements:
        with st.form("interview-opening-form"):
            opening = st.text_area(
                "本人役の開始発言",
                value=demo["interview"]["opening"],
                max_chars=1500,
            )
            ask = st.form_submit_button(
                "不足情報を一つ質問（APIを使用）",
                disabled=active_job_exists(services),
            )
        if ask:
            try:
                segment = _register_interview_segment(
                    services, "聞き取り記録1（開始発言）", "operator", opening
                )
                statements.append(segment)
                _enqueue_interview(services, item, statements)
                st.rerun()
            except Exception as exc:
                show_action_error(exc)
        return

    interview_outcome = st.session_state.get("last_outcomes", {}).get("interview")
    if interview_outcome:
        st.info(f"LLMの追加質問：{interview_outcome['question']}")
        answer_count = sum(
            1
            for statement in statements
            if statement["speaker"] == "operator" and statement is not statements[0]
        )
        default_reply = (
            demo["interview"]["reason_reply"]
            if answer_count == 0
            else demo["interview"]["scope_reply"]
        )
        with st.form(f"interview-reply-{answer_count}"):
            answer = st.text_area("本人役の回答", value=default_reply, max_chars=1500)
            reply = st.form_submit_button(
                "回答して次の質問へ（APIを使用）",
                disabled=active_job_exists(services),
            )
        if reply:
            try:
                exchange = _register_interview_exchange(
                    services,
                    str(interview_outcome["question"]),
                    answer,
                )
                statements.extend(exchange)
                _enqueue_interview(services, item, statements)
                st.rerun()
            except Exception as exc:
                show_action_error(exc)

    answer_count = max(
        0,
        sum(1 for statement in statements if statement["speaker"] == "operator") - 1,
    )
    st.caption(f"本人役の回答済み：{answer_count}回。回数で完了を強制しません。")
    if st.button(
        "ここまでの聞き取りで補足案を作る（APIを使用）",
        disabled=active_job_exists(services) or answer_count < 1,
    ):
        allowed = [
            str(statement["segment_id"])
            for statement in statements
            if statement["speaker"] == "operator"
        ]
        enqueue(
            services,
            mode="supplement",
            payload={
                "target_item_id": item.id,
                "target_version": item.version,
                "statements": statements,
                "allowed_segment_ids": allowed,
            },
        )
        st.session_state.nav_page = "更新案・実回答比較"
        st.rerun()


def _enqueue_interview(
    services: Services, item: KnowledgeRecord, statements: list[dict[str, str]]
) -> None:
    draft = {
        "facts": [
            {
                "kind": fact.kind.value,
                "text": fact.text,
                "condition_scope": (fact.condition_scope.value if fact.condition_scope else None),
                "parent_action_fact_id": fact.parent_action_fact_id,
                "evidence": [
                    {"segment_id": ref.segment_id, "quote": ref.quote} for ref in fact.evidence_refs
                ],
            }
            for fact in item.facts
        ],
        "cause_status": item.cause_status.value,
        "missing_fields": item.missing_fields,
    }
    enqueue(
        services,
        mode="interview",
        payload={"draft": draft, "statements": statements},
    )


def _register_interview_segment(
    services: Services, title: str, speaker: str, text: str
) -> dict[str, str]:
    _, mapping = services.repository.register_source(
        st.session_state.workspace_id,
        title=title,
        kind="interview",
        equipment="冷却器1",
        case_label="事例1",
        external_key=f"interview-{uuid4()}",
        segments=[(f"interview-segment-{uuid4()}", speaker, text)],
    )
    return {
        "segment_id": next(iter(mapping.values())),
        "text": text,
        "speaker": speaker,
    }


def _register_interview_exchange(
    services: Services, question: str, answer: str
) -> list[dict[str, str]]:
    question_key = f"interview-question-{uuid4()}"
    answer_key = f"interview-answer-{uuid4()}"
    _, mapping = services.repository.register_source(
        st.session_state.workspace_id,
        title="聞き取り記録1（質問と本人回答）",
        kind="interview",
        equipment="冷却器1",
        case_label="事例1",
        external_key=f"interview-exchange-{uuid4()}",
        segments=[
            (question_key, "assistant", question),
            (answer_key, "operator", answer),
        ],
    )
    return [
        {"segment_id": mapping[question_key], "text": question, "speaker": "assistant"},
        {"segment_id": mapping[answer_key], "text": answer, "speaker": "operator"},
    ]


def _enqueue_comparison(
    services: Services, question: str, item: KnowledgeRecord, stage: str
) -> None:
    conversation_id = services.repository.create_conversation(st.session_state.workspace_id)
    st.session_state.conversation_id = conversation_id
    state = services.conversations.prepare_turn(
        st.session_state.workspace_id,
        conversation_id,
        question,
        selected_knowledge_id=item.id,
    )
    services.repository.append_message(
        st.session_state.workspace_id,
        conversation_id,
        role="user",
        text=question,
    )
    enqueue(
        services,
        mode="qa",
        payload={
            "question": question,
            "consultation": state.model_dump(mode="json"),
            "comparison_stage": stage,
            "empty_history": True,
            "target_item_id": item.id,
            "target_version": item.version,
        },
    )
