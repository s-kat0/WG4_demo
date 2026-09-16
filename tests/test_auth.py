from __future__ import annotations

import pytest

from wg4_demo.auth import AuthService
from wg4_demo.errors import AuthenticationError, AuthorizationError
from wg4_demo.schemas import Role


def test_roles_are_distinct_and_logout_revokes(auth: AuthService) -> None:
    participant = auth.login("participant-secret", role=Role.PARTICIPANT, client_token="browser-a")
    admin = auth.login("admin-secret", role=Role.ADMIN, client_token="browser-admin")

    assert participant.id != admin.id
    assert auth.require_session(participant.id).role is Role.PARTICIPANT
    with pytest.raises(AuthorizationError):
        auth.require_session(participant.id, role=Role.ADMIN)

    auth.logout(participant.id)
    with pytest.raises(AuthenticationError, match="session_invalid"):
        auth.require_session(participant.id)


def test_wrong_password_and_missing_default_are_rejected(auth: AuthService) -> None:
    with pytest.raises(AuthenticationError, match="authentication_failed"):
        auth.login("wrong", role=Role.PARTICIPANT, client_token="browser-wrong")


def test_five_failures_trigger_cooldown(auth: AuthService) -> None:
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            auth.login("wrong", role=Role.PARTICIPANT, client_token="browser-brute")
    with pytest.raises(AuthenticationError, match="authentication_cooldown"):
        auth.login("participant-secret", role=Role.PARTICIPANT, client_token="browser-brute")
