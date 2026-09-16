from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import streamlit as st

from wg4_demo.safe_display import safe_error
from wg4_demo.schemas import Role
from wg4_demo.services import Services, build_services
from wg4_demo.settings import settings_from_mapping
from wg4_demo.ui import admin, knowledge, login, qa, register, review
from wg4_demo.ui.common import PAGES, apply_navigation_request, render_job_status

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _configuration_mapping() -> Mapping[str, object]:
    environment = os.environ.get("APP_ENV", "local")
    if environment == "cloud":
        return st.secrets
    return os.environ


@st.cache_resource
def _services() -> Services:
    settings = settings_from_mapping(_configuration_mapping())
    return build_services(settings, project_root=PROJECT_ROOT)


def main() -> None:
    st.set_page_config(page_title="現場知識をつなぐミニエージェント", page_icon="🔗", layout="wide")
    st.title("現場知識をつなぐミニエージェント")
    st.caption("AIエージェントによる暗黙知と形式知の構造化・活用の第一歩")
    st.warning("架空データによる機能デモです。実設備の診断・操作指示には使用しないでください。")
    st.info(
        "入力はOpenAI APIへ送信されます。機密情報・個人情報・実際の現場記録を入力しないでください。"
    )
    try:
        services = _services()
    except Exception as exc:
        code, message = safe_error(exc)
        st.error(f"起動設定を確認してください：{message}（{code}）")
        st.stop()
    session_id = st.session_state.get("session_id")
    if not session_id:
        login.render_login(services, PROJECT_ROOT)
        st.stop()
    try:
        services.auth.require_session(session_id, role=Role.PARTICIPANT)
        workspace = services.repository.require_workspace(session_id, st.session_state.workspace_id)
    except Exception:
        for key in ["session_id", "workspace_id", "conversation_id", "active_job_id"]:
            st.session_state.pop(key, None)
        st.error("セッションが無効です。再ログインしてください。")
        st.stop()
    with st.sidebar:
        st.caption("この作業領域は自分の練習用です。他の参加者には反映されません。")
        st.caption("Cloud上の一時保存です。必要な結果はJSONで保存してください。")
        st.caption(
            f"モデル: {services.settings.openai_model or '未設定'} / "
            f"推論: {services.settings.openai_reasoning_effort} / "
            f"KB改訂: {workspace.kb_revision} / 会話: {st.session_state.conversation_id[:8]}"
        )
        with st.expander("主実演の流れ", expanded=True):
            st.markdown(
                """
1. **知識を探す**：初期12件を非課金で検索し、原文を見る
2. **エージェントに相談する**：根拠付き候補へ追質問する
3. **知識を追加・補足する**：文書版A → 本人役の理由・範囲
4. **更新案・実回答比較**：人が承認し、A/B/Cの実記録を比べる

四段階ガイドは同じ処理への案内です。未承認情報は通常検索へ入りません。
                """
            )
        apply_navigation_request()
        if st.session_state.get("nav_page") not in PAGES:
            st.session_state.nav_page = PAGES[0]
        page = st.radio(
            "画面",
            PAGES,
            key="nav_page",
        )
        if st.button("ログアウト"):
            services.auth.logout(session_id)
            st.session_state.clear()
            st.rerun()
    render_job_status(services)
    if page == "知識を探す":
        knowledge.render(services, PROJECT_ROOT)
    elif page == "エージェントに相談する":
        qa.render(services, PROJECT_ROOT)
    elif page == "知識を追加・補足する":
        register.render(services, PROJECT_ROOT)
    elif page == "更新案・実回答比較":
        review.render(services, PROJECT_ROOT)
    else:
        admin.render(services)
