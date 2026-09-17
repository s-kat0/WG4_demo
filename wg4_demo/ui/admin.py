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
        with st.form("admin-login", clear_on_submit=True):
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
    status_summary = {
        "enabled": status["enabled"],
        "budget_mode": status["budget_mode"],
        "used_calls": status["used_calls"],
        "active_calls": status["active_calls"],
    }
    if status["budget_mode"] == "finite":
        status_summary["allocated_calls"] = status["allocated_calls"]
    st.write(status_summary)
    if status["budget_mode"] == "finite":
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
    else:
        st.info(
            "OpenAIプロジェクト側のhard limitを費用上限として使用します。"
            "アプリ内の全体・session別call枠は適用しません。使用回数は監査用に記録します。"
        )
        if not status["enabled"]:
            with st.form("resume-provider-limit"):
                confirmed = st.checkbox("専用OpenAIプロジェクトの強制停止型支出上限を確認した")
                reason = st.text_input("再開理由", value="OpenAI側hard limitを確認して再開")
                resume = st.form_submit_button("LLM要求を再開")
            if resume:
                try:
                    services.ledger.resume_with_provider_limit(
                        admin_session,
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
