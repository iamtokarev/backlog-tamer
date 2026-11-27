from __future__ import annotations

import pytest
from pydantic import ValidationError

from backlog_tamer.config import Settings


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in (
        "OPENROUTER_API_KEY",
        "TRIAGE__DECISION_MODEL",
        "TRIAGE__TITLE_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-browserbase-key")
    monkeypatch.setenv("TELEGRAM__BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM__ALLOWED_USER_ID", "42")
    monkeypatch.setenv("NOTION_PROJECTS_DATABASE_ID", "test-projects-db")
    monkeypatch.setenv("NOTION_TASKS_DATABASE_ID", "test-tasks-db")
    return tmp_path / ".env"


def test_settings_load_the_openrouter_key_with_default_triage_models(settings_env):
    settings_env.write_text("OPENROUTER_API_KEY=test-openrouter-key\n")

    settings = Settings()

    assert settings.openrouter_api_key.get_secret_value() == "test-openrouter-key"
    assert settings.triage.decision_model == "perplexity/pplx-decider-v1.3:free"
    assert settings.triage.title_model == "openai/gpt-6-luna"


@pytest.mark.parametrize("key", [None, "", "   "])
def test_openrouter_key_is_required(settings_env, monkeypatch, key):
    if key is not None:
        monkeypatch.setenv("OPENROUTER_API_KEY", key)

    with pytest.raises(ValidationError) as error:
        Settings()

    assert error.value.errors()[0]["loc"] == ("openrouter_api_key",)


def test_environment_overrides_dotenv_key_and_triage_models(settings_env, monkeypatch):
    settings_env.write_text(
        "OPENROUTER_API_KEY=dotenv-key\n"
        "TRIAGE__DECISION_MODEL=perplexity/pplx-decider-v1.2\n"
        "TRIAGE__TITLE_MODEL=openai/gpt-5.6-luna\n"
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-key")
    monkeypatch.setenv("TRIAGE__DECISION_MODEL", "perplexity/pplx-decider-v1.3")
    monkeypatch.setenv("TRIAGE__TITLE_MODEL", "anthropic/claude-sonnet-5")

    settings = Settings()

    assert settings.openrouter_api_key.get_secret_value() == "environment-key"
    assert settings.triage.decision_model == "perplexity/pplx-decider-v1.3"
    assert settings.triage.title_model == "anthropic/claude-sonnet-5"


def test_nested_dotenv_triage_model_overrides(settings_env):
    settings_env.write_text(
        "OPENROUTER_API_KEY=dotenv-key\n"
        "TRIAGE__DECISION_MODEL=perplexity/pplx-decider-v1.2\n"
        "TRIAGE__TITLE_MODEL=anthropic/claude-sonnet-5\n"
    )

    settings = Settings()

    assert settings.triage.decision_model == "perplexity/pplx-decider-v1.2"
    assert settings.triage.title_model == "anthropic/claude-sonnet-5"


@pytest.mark.parametrize("key", [None, "", "   "])
def test_browserbase_key_is_required(settings_env, monkeypatch, key):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("BROWSERBASE_API_KEY", key)
    with pytest.raises(ValidationError, match="browserbase_api_key"):
        Settings()
