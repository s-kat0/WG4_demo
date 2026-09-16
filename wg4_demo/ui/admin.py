from __future__ import annotations

import secrets

import streamlit as st

from wg4_demo.schemas import Role
from wg4_demo.services import Services
from wg4_demo.ui.common import show_action_error


def render(services: Services) -> None:
    st.header("管理")
    admin_session = st.session_state.get("admin_session_id")
    if admin_session:
        try:
            services.auth.require_session(admin_session, role=Role.ADMIN)
        except Exception:
            st.session_state.admin_session_id = None
            admin_session = None
    if not admin_session:
        with st.form("admin-login"):
            password = st.text_input("管理者パスワード", type="password")
            submitted = st.form_submit_button("管理者として認証")
        if submitted:
            try:
                session = services.auth.login(
                    password,
                    role=Role.ADMIN,
                    client_token=st.session_state.setdefault(
                        "admin_client_token", secrets.token_urlsafe(18)
                    ),
                )
                st.session_state.admin_session_id = session.id
                st.rerun()
            except Exception as exc:
                show_action_error(exc)
        return
    status = services.ledger.status()
    st.write(
        {
            "enabled": status["enabled"],
            "allocated_calls": status["allocated_calls"],
            "used_calls": status["used_calls"],
            "active_calls": status["active_calls"],
        }
    )
    with st.form("enable-budget"):
        amount = st.number_input("追加する有限call枠", min_value=1, max_value=600, value=30)
        confirmed = st.checkbox("専用OpenAIプロジェクトの強制停止型支出上限を確認した")
        reason = st.text_input("理由", value="講演用の有限枠")
        enable = st.form_submit_button("枠を追加して有効化")
    if enable:
        try:
            services.ledger.enable_budget(
                admin_session,
                additional_calls=int(amount),
                confirmed_external_limit=confirmed,
                reason=reason,
            )
            st.rerun()
        except Exception as exc:
            show_action_error(exc)
    if st.button("新しいLLM要求を停止"):
        try:
            services.ledger.stop(admin_session, reason="operator stop from admin UI")
            st.rerun()
        except Exception as exc:
            show_action_error(exc)
