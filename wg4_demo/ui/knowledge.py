from __future__ import annotations

from pathlib import Path

import streamlit as st

from wg4_demo.services import Services
from wg4_demo.ui.common import active_job_exists, show_action_error


def render(services: Services, project_root: Path) -> None:
    st.header("2. 知識を確認")
    try:
        items = services.repository.list_knowledge(st.session_state.workspace_id)
        for item in items:
            with st.expander(f"{item.display_name} v{item.version} — {item.equipment}"):
                st.caption(f"事例: {item.case_label or '一般'} / 原因: {item.cause_status.value}")
                for fact in item.facts:
                    st.write(f"{fact.kind.value}: {fact.text}")
                    for ref in fact.evidence_refs:
                        st.text(f"根拠: {ref.quote}")
                if item.missing_fields:
                    st.write("未確認事項: " + "、".join(item.missing_fields))
                try:
                    context = services.graph.get_context(
                        st.session_state.workspace_id, item.id, item.version
                    )
                    st.graphviz_chart(context.dot)
                except Exception:
                    st.warning("関係図を表示できません。保存済み知識と原文は変更されていません。")
        export_bytes = services.exporter.export_json(
            session_id=st.session_state.session_id,
            workspace_id=st.session_state.workspace_id,
        )
        st.download_button(
            "自分の承認済み知識をJSON保存",
            data=export_bytes,
            file_name="wg4-approved-knowledge.json",
            mime="application/json",
        )
        with st.expander("この作業領域を初期状態へ戻す"):
            confirmed = st.checkbox(
                "自分の知識・出典・提案・会話が削除され、利用回数は戻らないことを確認"
            )
            if st.button(
                "知識を最初に戻す",
                disabled=not confirmed or active_job_exists(services),
            ):
                workspace = services.repository.require_workspace(
                    st.session_state.session_id, st.session_state.workspace_id
                )
                _, conversation = services.repository.reset_workspace(
                    st.session_state.session_id,
                    st.session_state.workspace_id,
                    seed_mode=workspace.seed_mode,
                    seed_path=project_root / "data" / "approved_seed.json",
                )
                st.session_state.conversation_id = conversation
                st.session_state.last_outcomes = {}
                st.rerun()
    except Exception as exc:
        show_action_error(exc)
