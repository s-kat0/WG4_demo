from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import streamlit as st

from wg4_demo.services import Services
from wg4_demo.ui.common import active_job_exists, enqueue, show_action_error


def render(services: Services, project_root: Path) -> None:
    st.header("4. 更新案を確認")
    demo = json.loads((project_root / "data" / "demo_inputs.json").read_text("utf-8"))
    items = services.repository.list_knowledge(st.session_state.workspace_id)
    labels = {f"{item.display_name} v{item.version}": item for item in items}
    with st.form("update-form"):
        selected_label = st.selectbox("更新対象", list(labels)) if labels else None
        statement = st.text_area(
            "新しい発言",
            value=demo["update_statement"],
            max_chars=services.settings.max_input_chars,
        )
        submit = st.form_submit_button(
            "更新案を作る", disabled=active_job_exists(services) or not labels
        )
    if submit and selected_label:
        try:
            item = labels[selected_label]
            _, mapping = services.repository.register_source(
                st.session_state.workspace_id,
                title="聞き取り記録2",
                kind="interview",
                equipment=item.equipment,
                case_label=item.case_label,
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
                    "target_knowledge_id": item.id,
                    "target_version": item.version,
                    "equipment": item.equipment,
                },
            )
            st.rerun()
        except Exception as exc:
            show_action_error(exc)
    st.subheader("人の確認待ち")
    try:
        for proposal in services.repository.list_pending_proposals(st.session_state.workspace_id):
            with st.container(border=True):
                st.write(f"理由: {proposal.reason}")
                st.caption(
                    f"base v{proposal.base_version} / hash {proposal.content_hash[:12]} / pending"
                )
                for operation in proposal.operations:
                    if operation.new_fact:
                        st.write(
                            f"{operation.operation.value}: "
                            f"{operation.new_fact.kind.value} — {operation.new_fact.text}"
                        )
                    else:
                        st.write(f"{operation.operation.value}: {operation.target_fact_id}")
                approve, reject = st.columns(2)
                if approve.button(
                    "承認して新版にする",
                    key=f"approve-{proposal.id}",
                    disabled=active_job_exists(services),
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
                    "却下",
                    key=f"reject-{proposal.id}",
                    disabled=active_job_exists(services),
                ):
                    services.approvals.reject(
                        session_id=st.session_state.session_id,
                        workspace_id=st.session_state.workspace_id,
                        proposal_id=proposal.id,
                    )
                    st.rerun()
    except Exception as exc:
        show_action_error(exc)
