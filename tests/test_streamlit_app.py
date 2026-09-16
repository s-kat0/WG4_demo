from __future__ import annotations

from pathlib import Path

from argon2 import PasswordHasher
from streamlit.testing.v1 import AppTest


def test_unauthenticated_app_shows_only_safe_login_surface(project_root: Path, monkeypatch) -> None:
    for key in [
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "DEMO_PASSWORD_HASH",
        "ADMIN_PASSWORD_HASH",
        "AUTH_VERSION",
        "DEMO_EXPIRES_AT",
        "GLOBAL_TPM",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_ENV", "test")

    app = AppTest.from_file(str(project_root / "app.py"), default_timeout=10).run()

    assert not app.exception
    assert app.title[0].value == "現場知識をつなぐミニエージェント"
    assert any(widget.label == "共通パスワード" for widget in app.text_input)
    assert not app.download_button


def test_authenticated_navigation_keeps_api_disabled(project_root: Path, monkeypatch) -> None:
    hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
    values = {
        "APP_ENV": "test",
        "OPENAI_API_KEY": "FAKE_UI_CANARY_DO_NOT_LOG",
        "OPENAI_MODEL": "test-model",
        "DEMO_PASSWORD_HASH": hasher.hash("participant-ui-password"),
        "ADMIN_PASSWORD_HASH": hasher.hash("different-admin-ui-password"),
        "AUTH_VERSION": "ui-test-v1",
        "DEMO_EXPIRES_AT": "2099-01-01T00:00:00+00:00",
        "APP_LLM_ENABLED": "false",
        "GLOBAL_TPM": "100000",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    from wg4_demo.ui.app import _services

    _services.clear()

    app = AppTest.from_file(str(project_root / "app.py"), default_timeout=10).run()
    password = next(widget for widget in app.text_input if widget.label == "共通パスワード")
    password.input("participant-ui-password")
    next(button for button in app.button if button.label == "ログイン").click()
    app.run()

    assert not app.exception
    navigation = next(widget for widget in app.radio if widget.label == "画面")
    navigation.set_value("知識を確認")
    app.run()
    assert not app.exception
    assert app.header[0].value == "2. 知識を確認"
    assert app.download_button
