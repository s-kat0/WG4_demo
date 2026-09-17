from __future__ import annotations

import secrets
from pathlib import Path

import streamlit as st

from wg4_demo.schemas import Role
from wg4_demo.services import Services
from wg4_demo.ui.common import show_action_error


def render_login(services: Services, project_root: Path) -> None:
    st.subheader("参加者ログイン")
    st.caption("ログインすると、初期知識12件を収録した自分専用の練習領域を新しく作成します。")
    with st.form("login-form", clear_on_submit=True):
        password = st.text_input("共通パスワード", type="password")
        submitted = st.form_submit_button("ログイン")
    if not submitted:
        return
    try:
        client_token = st.session_state.setdefault("login_client_token", secrets.token_urlsafe(18))
        session = services.auth.login(
            password,
            role=Role.PARTICIPANT,
            client_token=client_token,
        )
        workspace = services.repository.create_workspace(
            session.id,
            seed_mode="practical_v5",
            seed_path=project_root / "data" / "knowledge_seed_v5.json",
        )
        conversation = services.repository.create_conversation(workspace.id)
        st.session_state.session_id = session.id
        st.session_state.workspace_id = workspace.id
        st.session_state.conversation_id = conversation
        st.session_state.last_outcomes = {}
        st.session_state.last_outcome_action_ids = {}
        st.session_state.active_job_id = None
        st.session_state.nav_page = "知識を探す"
        st.session_state.operator_view = False
        st.rerun()
    except Exception as exc:
        show_action_error(exc)
