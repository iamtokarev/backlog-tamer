from __future__ import annotations

import pytest
from pydantic import ValidationError

from backlog_tamer.config import Settings


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in (
        "OPENROUTER_API_KEY",
        "AGENT__OPENAI_API_KEY",
        "AGENT__MODEL",
        "AGENT__REASONING_EFFORT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM__BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM__ALLOWED_USER_ID", "42")
    monkeypatch.setenv("NOTION_PROJECTS_DATABASE_ID", "test-projects-db")
    monkeypatch.setenv("NOTION_TASKS_DATABASE_ID", "test-tasks-db")
    return tmp_path / ".env"


def test_settings_load_existing_openrouter_key_without_agent_overrides(settings_env):
    settings_env.write_text("OPENROUTER_API_KEY=test-openrouter-key\n")

    settings = Settings()

    assert settings.openrouter_api_key.get_secret_value() == "test-openrouter-key"
    assert settings.agent.model == "openai/gpt-6-luna"
    assert settings.agent.reasoning_effort == "medium"


@pytest.mark.parametrize("key", [None, "", "   "])
def test_openrouter_key_is_required_even_with_legacy_openai_key(
    settings_env, monkeypatch, key
):
    monkeypatch.setenv("AGENT__OPENAI_API_KEY", "test-legacy-key")
    if key is not None:
        monkeypatch.setenv("OPENROUTER_API_KEY", key)

    with pytest.raises(ValidationError) as error:
        Settings()

    assert error.value.errors()[0]["loc"] == ("openrouter_api_key",)


def test_environment_overrides_dotenv_key_model_and_reasoning(
    settings_env, monkeypatch
):
    settings_env.write_text(
        "OPENROUTER_API_KEY=dotenv-key\n"
        "AGENT__MODEL=openai/gpt-5.6-luna\n"
        "AGENT__REASONING_EFFORT=medium\n"
        "AGENT__OPENAI_API_KEY=ignored-legacy-key\n"
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-key")
    monkeypatch.setenv("AGENT__MODEL", "anthropic/claude-sonnet-5")
    monkeypatch.setenv("AGENT__REASONING_EFFORT", "low")

    settings = Settings()

    assert settings.openrouter_api_key.get_secret_value() == "environment-key"
    assert settings.agent.model == "anthropic/claude-sonnet-5"
    assert settings.agent.reasoning_effort == "low"


def test_nested_dotenv_model_and_reasoning_overrides(settings_env):
    settings_env.write_text(
        "OPENROUTER_API_KEY=dotenv-key\n"
        "AGENT__MODEL=anthropic/claude-sonnet-5\n"
        "AGENT__REASONING_EFFORT=none\n"
    )

    settings = Settings()

    assert settings.agent.model == "anthropic/claude-sonnet-5"
    assert settings.agent.reasoning_effort == "none"
