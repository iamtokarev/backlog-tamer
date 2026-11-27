from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backlog_tamer.integrations.telegram.config import TelegramConfig


class TriageConfig(BaseSettings):
    """The OpenRouter models Triage drafts and revises an Intake with.

    The decision model answers Triage's typed questions; the title model
    writes the project name. Override with TRIAGE__DECISION_MODEL and
    TRIAGE__TITLE_MODEL.
    """

    # Pin a dated model: decision probabilities, and so thresholds, move
    # between versions. v1.3 is only listed as :free on OpenRouter for now.
    decision_model: str = "perplexity/pplx-decider-v1.3:free"
    title_model: str = "openai/gpt-6-luna"

    model_config = SettingsConfigDict(extra="ignore")


class Settings(BaseSettings):
    openrouter_api_key: SecretStr
    browserbase_api_key: SecretStr
    triage: TriageConfig = Field(default_factory=TriageConfig)
    telegram: TelegramConfig

    database_url: str = "sqlite:///backlog_tamer.db"
    notion_token: SecretStr | None = None
    notion_projects_database_id: str
    notion_tasks_database_id: str
    notion_api_version: str = "2022-06-28"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @field_validator("openrouter_api_key", "browserbase_api_key")
    @classmethod
    def validate_provider_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Provider API key must not be blank.")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
