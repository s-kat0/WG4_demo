from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

import streamlit as st

from wg4_demo.repository import KnowledgeRecord
from wg4_demo.services import Services
from wg4_demo.ui.common import active_job_exists, navigate_to, show_action_error

SOURCE_LABELS: dict[str, Literal["document", "interview", "mixed"] | None] = {
    "すべて": None,
    "文書": "document",
    "Q&A": "interview",
    "文書＋Q&A": "mixed",
}


def render(services: Services, project_root: Path) -> None:
    st.header("1. 知識を探す")
    st.caption("承認済み知識だけを対象にした非課金の通常検索です。画面表示ではAPIを呼びません。")
    try:
        workspace_id = st.session_state.workspace_id
        items = services.repository.list_knowledge(workspace_id)
        stats = services.repository.knowledge_stats(workspace_id)
        source_counts = cast(dict[str, int], stats["source_counts"])
        columns = st.columns(3)
        columns[0].metric("知識項目", cast(int, stats["knowledge_count"]))
        columns[1].metric(
            "由来",
            f"文書 {source_counts.get('document', 0)} / "
            f"Q&A {source_counts.get('interview', 0)} / "
            f"混合 {source_counts.get('mixed', 0)}",
        )
        columns[2].metric("設備", cast(int, stats["equipment_count"]))

        equipment_options = ["すべて", *sorted({item.equipment for item in items})]
        with st.form("knowledge-search"):
            query = st.text_input("検索語", value="冷却器1 流量低下")
            filter_columns = st.columns(2)
            equipment = filter_columns[0].selectbox("設備", equipment_options)
            origin = filter_columns[1].selectbox("由来", list(SOURCE_LABELS))
            submitted = st.form_submit_button("検索する（API不使用）")
        if submitted or "search_result" not in st.session_state:
            result = services.retrieval.search_knowledge(
                workspace_id,
                query,
                equipment_name=None if equipment == "すべて" else equipment,
                source_kind=SOURCE_LABELS[origin],
                limit=20,
            )
            st.session_state.search_result = result.model_dump(mode="json")
        result_payload = st.session_state.get("search_result")
        if not result_payload:
            return
        hits = result_payload["hits"]
        st.subheader(f"検索結果：{len(hits)}件")
        if not hits:
            st.info("検索は正常に完了しましたが、該当する承認済み知識は0件です。")
        for hit in hits:
            item = services.repository.get_knowledge(
                workspace_id, hit["knowledge_id"], hit["version"]
            )
            _render_hit(services, item, score=float(hit["score"]))

        with st.expander("承認済み知識の保存・領域の初期化"):
            export_bytes = services.exporter.export_json(
                session_id=st.session_state.session_id,
                workspace_id=workspace_id,
            )
            st.download_button(
                "自分の承認済み知識をJSON保存",
                data=export_bytes,
                file_name="wg4-approved-knowledge.json",
                mime="application/json",
            )
            confirmed = st.checkbox(
                "自分の知識・出典・提案・会話が削除され、利用回数は戻らないことを確認"
            )
            if st.button(
                "この領域を初期状態へ戻す",
                disabled=not confirmed or active_job_exists(services),
            ):
                workspace = services.repository.require_workspace(
                    st.session_state.session_id, workspace_id
                )
                seed_path = (
                    project_root / "data" / "knowledge_seed_v5.json"
                    if workspace.seed_mode == "practical_v5"
                    else (
                        project_root / "data" / "approved_seed.json"
                        if workspace.seed_mode == "approved_v1"
                        else None
                    )
                )
                _, conversation = services.repository.reset_workspace(
                    st.session_state.session_id,
                    workspace_id,
                    seed_mode=workspace.seed_mode,
                    seed_path=seed_path,
                )
                st.session_state.conversation_id = conversation
                st.session_state.last_outcomes = {}
                st.session_state.last_outcome_action_ids = {}
                st.session_state.pop("search_result", None)
                st.rerun()
    except Exception as exc:
        st.session_state.pop("search_result", None)
        show_action_error(exc)
        st.error("検索処理に失敗しました。正常な0件としては扱っていません。")


def _render_hit(services: Services, item: KnowledgeRecord, *, score: float) -> None:
    origin = {"document": "文書", "interview": "Q&A", "mixed": "文書＋Q&A"}[item.source_kind]
    with st.container(border=True):
        st.markdown(f"**{item.title}**")
        st.caption(
            f"{item.display_name} v{item.version} / {item.equipment} / {origin} / "
            f"{item.origin_label} / 関連度 {score:.3f}"
        )
        for fact in item.facts:
            st.write(f"- {fact.kind.value}: {fact.text}")
        if item.missing_fields:
            st.warning("未確認事項：" + "、".join(item.missing_fields))
        evidence_ids = list(
            dict.fromkeys(ref.segment_id for fact in item.facts for ref in fact.evidence_refs)
        )
        with st.expander("原文と版を確認"):
            for segment in services.repository.read_source_transcripts(
                item.workspace_id, evidence_ids
            ):
                st.caption(f"{segment['title']} / {segment['kind']} / 原文{segment['ordinal']}")
                evidence_label = (
                    "根拠" if segment["id"] in evidence_ids else "Q&A文脈（fact根拠ではない）"
                )
                st.text(f"[{segment['speaker']} / {evidence_label}] {segment['text']}")
        if st.button(
            "この知識について相談する",
            key=f"consult-{item.id}-{item.version}",
            disabled=active_job_exists(services),
        ):
            st.session_state.consult_knowledge_id = item.id
            st.session_state.consult_question = f"{item.title}について、何を確認すべきですか。"
            navigate_to("エージェントに相談する")
