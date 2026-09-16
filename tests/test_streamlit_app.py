from __future__ import annotations

from pathlib import Path

from argon2 import PasswordHasher
from streamlit.testing.v1 import AppTest

from wg4_demo.schemas import (
    CauseStatus,
    ConditionScope,
    EvidenceDraft,
    FactDraft,
    FactKind,
    OperationType,
    ProposalOperation,
)


def test_unauthenticated_app_shows_only_safe_login_surface(
    project_root: Path, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
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
    from wg4_demo.ui.app import _services

    _services().scheduler.stop()
    _services.clear()


def test_authenticated_navigation_keeps_api_disabled(
    project_root: Path, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
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
    navigation.set_value("知識を探す")
    app.run()
    assert not app.exception
    assert app.header[0].value == "1. 知識を探す"
    assert app.download_button
    assert any(button.label == "検索する（API不使用）" for button in app.button)

    navigation = next(widget for widget in app.radio if widget.label == "画面")
    navigation.set_value("更新案・実回答比較")
    app.run()
    assert not app.exception
    assert app.header[0].value == "4. 更新案・実回答比較"
    assert any(expander.label == "この画面で今すること" for expander in app.expander)
    assert any(
        "知識を追加・補足する" in markdown.value
        for markdown in app.markdown
        if isinstance(markdown.value, str)
    )
    services = _services()
    workspace_id = app.session_state["workspace_id"]
    _, mapping = services.repository.register_source(
        workspace_id,
        title="UIテスト保全記録",
        kind="document",
        equipment="冷却器1",
        case_label="事例1",
        external_key="ui-pending-source",
        segments=[
            (
                "ui-pending-p1",
                "document",
                "温度計交換後、出口温度表示が高め。別計器と照合した。原因は未特定。",
            )
        ],
    )
    segment_id = mapping["ui-pending-p1"]
    operations = [
        ProposalOperation(
            operation=OperationType.ADD_FACT,
            new_fact=FactDraft(
                kind=FactKind.OBSERVATION,
                text="出口温度表示が高め",
                evidence=[EvidenceDraft(segment_id=segment_id, quote="出口温度表示が高め")],
            ),
        ),
        ProposalOperation(
            operation=OperationType.ADD_FACT,
            new_fact=FactDraft(
                kind=FactKind.CONDITION,
                text="温度計交換後",
                condition_scope=ConditionScope.CASE_CONTEXT,
                evidence=[EvidenceDraft(segment_id=segment_id, quote="温度計交換後")],
            ),
        ),
        ProposalOperation(
            operation=OperationType.ADD_FACT,
            new_fact=FactDraft(
                kind=FactKind.CHECK_ACTION,
                text="別計器との照合",
                evidence=[EvidenceDraft(segment_id=segment_id, quote="別計器と照合した")],
            ),
        ),
        ProposalOperation(
            operation=OperationType.ADD_FACT,
            new_fact=FactDraft(
                kind=FactKind.CAUSE_STATUS,
                text="原因は未特定",
                evidence=[EvidenceDraft(segment_id=segment_id, quote="原因は未特定")],
            ),
        ),
    ]
    proposal = services.repository.stage_proposal(
        workspace_id,
        action_id="ui-pending-action",
        target_item_id=None,
        base_version=0,
        operations=operations,
        reason="UIで内容と根拠を確認する",
        equipment="冷却器1",
        case_label="事例1",
        missing_fields=["照合結果"],
        cause_status=CauseStatus.UNRESOLVED,
        allowed_segment_ids={segment_id},
    )
    services.repository.publish_proposal(workspace_id, proposal.id)
    app.run()

    assert not app.exception
    assert app.subheader[0].value == "人の確認待ち"
    assert any(expander.label == "原文と差分を照合" for expander in app.expander)
    assert any(
        checkbox.label == "追加内容、fact種別、確定度、未確認事項、原文を照合しました"
        for checkbox in app.checkbox
    )
    approve_button = next(button for button in app.button if button.label == "確認してv1として承認")
    assert approve_button.disabled is True
    services.scheduler.stop()
    _services.clear()
