from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    openai_api_key: SecretStr
    model: str = "gpt-5.4-mini"


class Settings(BaseSettings):
    agent: AgentConfig

    database_url: str = "sqlite:///backlog_tamer.db"

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
