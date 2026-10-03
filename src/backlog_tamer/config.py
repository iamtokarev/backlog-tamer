import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backlog_tamer.integrations.telegram.config import TelegramConfig


class AgentConfig(BaseSettings):
    model: str = "openai/gpt-5.6-luna"
    reasoning_effort: Literal["none", "low", "medium", "high"] = "medium"

    model_config = SettingsConfigDict(extra="ignore")


class Settings(BaseSettings):
    openrouter_api_key: SecretStr
    agent: AgentConfig = Field(default_factory=AgentConfig)
    telegram: TelegramConfig

    database_url: str = "sqlite:///backlog_tamer.db"
    notion_token: SecretStr | None = None
    notion_projects_database_id: str
    notion_tasks_database_id: str
    notion_api_version: str = "2022-06-28"

    langsmith_tracing: bool = True
    langsmith_endpoint: str = "https://eu.api.smith.langchain.com"
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "backlog-tamer"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @field_validator("openrouter_api_key")
    @classmethod
    def validate_openrouter_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("OPENROUTER_API_KEY must not be blank.")
        return value

    def export_to_env(self) -> None:
        if self.langsmith_api_key is not None:
            os.environ.setdefault(
                "LANGSMITH_TRACING",
                str(self.langsmith_tracing).lower(),
            )
            os.environ.setdefault(
                "LANGSMITH_API_KEY",
                self.langsmith_api_key.get_secret_value(),
            )
            os.environ.setdefault("LANGSMITH_ENDPOINT", self.langsmith_endpoint)
            os.environ.setdefault("LANGSMITH_PROJECT", self.langsmith_project)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
