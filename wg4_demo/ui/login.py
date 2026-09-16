from __future__ import annotations

import secrets
from pathlib import Path

import streamlit as st

from wg4_demo.schemas import Role
from wg4_demo.services import Services
from wg4_demo.ui.common import show_action_error


def render_login(services: Services, project_root: Path) -> None:
    st.subheader("参加者ログイン")
    seed_label = st.radio(
        "開始する練習領域",
        [
            "実務デモを開始（初期12件）",
            "旧デモ互換（確認済みv1サンプル）",
            "空の領域から開始",
        ],
        horizontal=True,
        help="既存の領域へseedを追加せず、ログイン時に作る新規領域だけを初期化します。",
    )
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
        if seed_label.startswith("実務"):
            mode = "practical_v5"
            seed_path = project_root / "data" / "knowledge_seed_v5.json"
        elif seed_label.startswith("旧"):
            mode = "approved_v1"
            seed_path = project_root / "data" / "approved_seed.json"
        else:
            mode = "from_scratch"
            seed_path = None
        workspace = services.repository.create_workspace(
            session.id,
            seed_mode=mode,
            seed_path=seed_path,
        )
        conversation = services.repository.create_conversation(workspace.id)
        st.session_state.session_id = session.id
        st.session_state.workspace_id = workspace.id
        st.session_state.conversation_id = conversation
        st.session_state.last_outcomes = {}
        st.session_state.last_outcome_action_ids = {}
        st.session_state.active_job_id = None
        st.session_state.nav_page = "知識を探す"
        st.rerun()
    except Exception as exc:
        show_action_error(exc)
