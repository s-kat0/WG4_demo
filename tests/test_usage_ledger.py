from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wg4_demo.auth import AuthService
from wg4_demo.errors import AppError, AuthorizationError, ConfigurationError
from wg4_demo.schemas import Role, SessionRecord
from wg4_demo.settings import Settings
from wg4_demo.usage_ledger import UsageLedger


def test_budget_starts_disabled_and_reservations_are_atomic(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    finite_settings = settings.model_copy(update={"call_budget_mode": "finite"})
    ledger = UsageLedger(finite_settings.control_db_path, finite_settings, auth)
    with pytest.raises(AppError, match="llm_disabled"):
        ledger.reserve_call(
            session_id=participant.id,
            action_id="action-1",
            model="test-model",
            estimated_input_tokens=10,
        )

    admin = auth.login("admin-secret", role=Role.ADMIN, client_token="admin-browser")
    ledger.enable_budget(
        admin.id,
        additional_calls=1,
        confirmed_external_limit=True,
        reason="test finite allocation",
    )
    reservation = ledger.reserve_call(
        session_id=participant.id,
        action_id="action-1",
        model="test-model",
        estimated_input_tokens=10,
    )
    ledger.finish_call(
        reservation,
        state="unknown",
        input_tokens=None,
        output_tokens=None,
        safe_error_code="timeout",
    )

    with pytest.raises(AppError, match="call_budget_exhausted"):
        ledger.reserve_call(
            session_id=participant.id,
            action_id="action-2",
            model="test-model",
            estimated_input_tokens=10,
        )
    assert ledger.status()["used_calls"] == 1


def test_unapproved_model_is_rejected_before_reservation(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    ledger = UsageLedger(settings.control_db_path, settings, auth)
    with pytest.raises(AppError, match="model_not_allowed"):
        ledger.reserve_call(
            session_id=participant.id,
            action_id="action-model",
            model="automatic-fallback-model",
            estimated_input_tokens=10,
        )
    assert ledger.status()["used_calls"] == 0


def test_runtime_live_gate_blocks_reservation_even_when_budget_is_enabled(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    disabled = settings.model_copy(update={"app_llm_enabled": False, "call_budget_mode": "finite"})
    ledger = UsageLedger(disabled.control_db_path, disabled, auth)
    admin = auth.login("admin-secret", role=Role.ADMIN, client_token="disabled-gate-admin")
    ledger.enable_budget(
        admin.id,
        additional_calls=1,
        confirmed_external_limit=True,
        reason="gate test",
    )

    with pytest.raises(ConfigurationError):
        ledger.reserve_call(
            session_id=participant.id,
            action_id="disabled-gate-action",
            model="test-model",
            estimated_input_tokens=10,
        )
    assert ledger.status()["used_calls"] == 0


def test_provider_hard_limit_mode_does_not_require_call_allocation(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    provider_settings = settings.model_copy(
        update={
            "call_budget_mode": "provider_hard_limit",
            "app_max_llm_calls": 1,
            "session_max_llm_calls": 1,
        }
    )
    ledger = UsageLedger(provider_settings.control_db_path, provider_settings, auth)

    for index in range(2):
        reservation = ledger.reserve_call(
            session_id=participant.id,
            action_id=f"provider-action-{index}",
            model="test-model",
            estimated_input_tokens=10,
        )
        ledger.finish_call(
            reservation,
            state="completed",
            input_tokens=10,
            output_tokens=1,
        )

    status = ledger.status()
    assert status["enabled"] is True
    assert status["budget_mode"] == "provider_hard_limit"
    assert status["allocated_calls"] == 0
    assert status["used_calls"] == 2


def test_schema_v1_finite_ledger_migrates_to_provider_hard_limit(
    settings: Settings, tmp_path: Path
) -> None:
    runtime_dir = tmp_path / "legacy-runtime"
    runtime_dir.mkdir()
    legacy_settings = settings.model_copy(update={"runtime_dir": runtime_dir})
    connection = sqlite3.connect(legacy_settings.control_db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE control_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                allocated_calls INTEGER NOT NULL CHECK (allocated_calls >= 0),
                hard_call_ceiling INTEGER NOT NULL CHECK (hard_call_ceiling > 0),
                auth_version TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO control_state VALUES (1, 1, 0, 0, 600, 'test-v1', 'legacy');
            """
        )
        connection.commit()
    finally:
        connection.close()

    auth = AuthService(legacy_settings.control_db_path, legacy_settings)
    ledger = UsageLedger(legacy_settings.control_db_path, legacy_settings, auth)

    status = ledger.status()
    assert status["enabled"] is True
    assert status["budget_mode"] == "provider_hard_limit"


def test_provider_mode_requires_admin_confirmation_after_stop(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    ledger = UsageLedger(settings.control_db_path, settings, auth)
    admin = auth.login("admin-secret", role=Role.ADMIN, client_token="provider-admin")
    ledger.stop(admin.id, reason="test stop")

    with pytest.raises(AppError, match="llm_disabled"):
        ledger.reserve_call(
            session_id=participant.id,
            action_id="stopped-action",
            model="test-model",
            estimated_input_tokens=10,
        )
    with pytest.raises(AuthorizationError) as exc_info:
        ledger.resume_with_provider_limit(
            admin.id,
            confirmed_external_limit=False,
            reason="must not resume",
        )
    assert "外部の強制停止型支出上限" in exc_info.value.user_message

    ledger.resume_with_provider_limit(
        admin.id,
        confirmed_external_limit=True,
        reason="provider limit confirmed",
    )
    assert ledger.status()["enabled"] is True
