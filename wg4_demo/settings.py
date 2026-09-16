"""Strict configuration loading without secret-bearing repr output."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from wg4_demo.errors import ConfigurationError

PLACEHOLDER_PREFIX = "REPLACE_WITH_"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    app_env: str
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "low"
    demo_password_hash: SecretStr | None = None
    admin_password_hash: SecretStr | None = None
    auth_version: str | None = None
    demo_expires_at: datetime | None = None
    app_llm_enabled: bool = False
    app_max_llm_calls: int = Field(default=600, ge=1)
    session_max_llm_calls: int = Field(default=40, ge=1)
    max_model_calls_per_action: int = Field(default=6, ge=1, le=20)
    max_tool_calls_per_action: int = Field(default=8, ge=1, le=32)
    max_concurrent_jobs: int = Field(default=3, ge=1, le=10)
    max_pending_jobs: int = Field(default=30, ge=1, le=100)
    max_active_jobs_per_session: int = Field(default=1, ge=1, le=1)
    max_concurrent_llm: int = Field(default=3, ge=1, le=10)
    queue_wait_timeout_seconds: int = Field(default=300, ge=1)
    action_timeout_seconds: int = Field(default=90, ge=1)
    job_status_poll_seconds: int = Field(default=2, ge=1)
    global_rpm: int = Field(default=60, ge=1)
    global_tpm: int | None = Field(default=None, ge=1)
    max_input_chars: int = Field(default=1500, ge=1)
    max_document_chars: int = Field(default=6000, ge=1)
    max_prompt_bytes: int = Field(default=64000, ge=1024)
    max_estimated_input_tokens: int = Field(default=16000, ge=1)
    max_output_tokens: int = Field(default=2048, ge=1)
    request_timeout_seconds: int = Field(default=35, ge=1)
    workspace_ttl_hours: int = Field(default=24, ge=1)
    runtime_dir: Path = Path("runtime")

    @field_validator("app_env")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        if value not in {"local", "cloud", "test"}:
            raise ValueError("APP_ENV must be local, cloud, or test")
        return value

    @field_validator("demo_expires_at")
    @classmethod
    def validate_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("DEMO_EXPIRES_AT must include a timezone")
        return value

    @field_validator("openai_model", "auth_version")
    @classmethod
    def reject_text_placeholder(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value.startswith(PLACEHOLDER_PREFIX)):
            return None
        return value

    @field_validator("openai_api_key", "demo_password_hash", "admin_password_hash")
    @classmethod
    def reject_secret_placeholder(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if not raw or raw.startswith(PLACEHOLDER_PREFIX):
            return None
        return value

    @model_validator(mode="after")
    def validate_hashes_differ(self) -> Settings:
        if (
            self.demo_password_hash
            and self.admin_password_hash
            and self.demo_password_hash.get_secret_value()
            == self.admin_password_hash.get_secret_value()
        ):
            raise ValueError("participant and admin password hashes must differ")
        return self

    @property
    def knowledge_db_path(self) -> Path:
        return self.runtime_dir / "knowledge.sqlite3"

    @property
    def control_db_path(self) -> Path:
        return self.runtime_dir / "control.sqlite3"

    def live_blockers(self, *, now: datetime) -> list[str]:
        blockers: list[str] = []
        if not self.app_llm_enabled:
            blockers.append("APP_LLM_ENABLED=false")
        if self.openai_api_key is None:
            blockers.append("OPENAI_API_KEY is not configured")
        if self.openai_model is None:
            blockers.append("OPENAI_MODEL is not configured")
        if self.global_tpm is None:
            blockers.append("GLOBAL_TPM is not configured")
        if self.auth_version is None:
            blockers.append("AUTH_VERSION is not configured")
        if self.demo_expires_at is None:
            blockers.append("DEMO_EXPIRES_AT is not configured")
        elif now >= self.demo_expires_at:
            blockers.append("DEMO_EXPIRES_AT has passed")
        return blockers


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip() or value.startswith(PLACEHOLDER_PREFIX):
        return None
    return int(value)


def settings_from_mapping(
    values: Mapping[str, object], *, runtime_dir: Path | None = None
) -> Settings:
    def raw(name: str) -> str | None:
        value = values.get(name)
        return None if value is None else str(value)

    payload: dict[str, object] = {
        "app_env": raw("APP_ENV") or "local",
        "openai_api_key": raw("OPENAI_API_KEY"),
        "openai_model": raw("OPENAI_MODEL"),
        "openai_reasoning_effort": raw("OPENAI_REASONING_EFFORT") or "low",
        "demo_password_hash": raw("DEMO_PASSWORD_HASH"),
        "admin_password_hash": raw("ADMIN_PASSWORD_HASH"),
        "auth_version": raw("AUTH_VERSION"),
        "demo_expires_at": raw("DEMO_EXPIRES_AT"),
        "app_llm_enabled": raw("APP_LLM_ENABLED") or "false",
        "global_tpm": _optional_int(raw("GLOBAL_TPM")),
    }
    integer_defaults = {
        "APP_MAX_LLM_CALLS": 600,
        "SESSION_MAX_LLM_CALLS": 40,
        "MAX_MODEL_CALLS_PER_ACTION": 6,
        "MAX_TOOL_CALLS_PER_ACTION": 8,
        "MAX_CONCURRENT_JOBS": 3,
        "MAX_PENDING_JOBS": 30,
        "MAX_ACTIVE_JOBS_PER_SESSION": 1,
        "MAX_CONCURRENT_LLM": 3,
        "QUEUE_WAIT_TIMEOUT_SECONDS": 300,
        "ACTION_TIMEOUT_SECONDS": 90,
        "JOB_STATUS_POLL_SECONDS": 2,
        "GLOBAL_RPM": 60,
        "MAX_INPUT_CHARS": 1500,
        "MAX_DOCUMENT_CHARS": 6000,
        "MAX_PROMPT_BYTES": 64000,
        "MAX_ESTIMATED_INPUT_TOKENS": 16000,
        "MAX_OUTPUT_TOKENS": 2048,
        "REQUEST_TIMEOUT_SECONDS": 35,
        "WORKSPACE_TTL_HOURS": 24,
    }
    for env_name, default in integer_defaults.items():
        payload[env_name.lower()] = int(raw(env_name) or default)
    if runtime_dir is not None:
        payload["runtime_dir"] = runtime_dir
    try:
        return Settings.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError("設定値が不正です。運営者が設定を確認してください。") from exc


def settings_from_environment(*, runtime_dir: Path | None = None) -> Settings:
    return settings_from_mapping(os.environ, runtime_dir=runtime_dir)
