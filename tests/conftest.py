from __future__ import annotations

from pathlib import Path

import pytest
from argon2 import PasswordHasher

from wg4_demo.auth import AuthService
from wg4_demo.repository import Repository
from wg4_demo.schemas import Role, SessionRecord
from wg4_demo.settings import Settings, settings_from_mapping


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
    return settings_from_mapping(
        {
            "APP_ENV": "test",
            "OPENAI_API_KEY": "FAKE_API_CANARY_DO_NOT_LOG_42",
            "OPENAI_MODEL": "test-model",
            "DEMO_PASSWORD_HASH": hasher.hash("participant-secret"),
            "ADMIN_PASSWORD_HASH": hasher.hash("admin-secret"),
            "AUTH_VERSION": "test-v1",
            "DEMO_EXPIRES_AT": "2099-01-01T00:00:00+00:00",
            "APP_LLM_ENABLED": "true",
            "GLOBAL_TPM": "100000",
        },
        runtime_dir=tmp_path / "runtime",
    )


@pytest.fixture
def auth(settings: Settings) -> AuthService:
    return AuthService(settings.control_db_path, settings)


@pytest.fixture
def participant(auth: AuthService) -> SessionRecord:
    return auth.login(
        "participant-secret", role=Role.PARTICIPANT, client_token="browser-participant"
    )


@pytest.fixture
def repository(settings: Settings) -> Repository:
    return Repository(settings.knowledge_db_path)
