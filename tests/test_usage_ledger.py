from __future__ import annotations

import pytest

from wg4_demo.auth import AuthService
from wg4_demo.errors import AppError
from wg4_demo.schemas import Role, SessionRecord
from wg4_demo.settings import Settings
from wg4_demo.usage_ledger import UsageLedger


def test_budget_starts_disabled_and_reservations_are_atomic(
    settings: Settings, auth: AuthService, participant: SessionRecord
) -> None:
    ledger = UsageLedger(settings.control_db_path, settings, auth)
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
