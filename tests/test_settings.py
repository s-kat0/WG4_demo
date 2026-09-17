from __future__ import annotations

import tomllib
from pathlib import Path

from dotenv import dotenv_values

from wg4_demo.settings import settings_from_mapping


def test_agent_model_budget_can_finish_after_all_permitted_tools(tmp_path: Path) -> None:
    settings = settings_from_mapping({"APP_ENV": "test"}, runtime_dir=tmp_path / "runtime")

    assert settings.max_model_calls_per_action == 12
    assert settings.max_tool_calls_per_action == 8
    assert settings.max_model_calls_per_action > settings.max_tool_calls_per_action


def test_cloud_and_env_examples_keep_the_same_agent_call_budget(project_root: Path) -> None:
    env_values = dotenv_values(project_root / ".env.example")
    cloud_values = tomllib.loads(
        (project_root / ".streamlit" / "secrets.example.toml").read_text("utf-8")
    )

    assert env_values["MAX_MODEL_CALLS_PER_ACTION"] == "12"
    assert cloud_values["MAX_MODEL_CALLS_PER_ACTION"] == 12
