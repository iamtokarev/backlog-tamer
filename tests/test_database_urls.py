from __future__ import annotations

import pytest

from backlog_tamer.application.database_urls import (
    to_adk_session_database_url,
    to_sync_database_url,
)


def test_sqlite_database_urls_are_converted_for_sync_and_adk_sessions():
    assert to_sync_database_url("sqlite+aiosqlite:///local.db") == "sqlite:///local.db"
    assert to_adk_session_database_url("sqlite:///local.db") == (
        "sqlite+aiosqlite:///local.db"
    )


def test_postgres_database_urls_are_converted_for_sync_and_adk_sessions():
    assert to_sync_database_url("postgresql://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert to_adk_session_database_url("postgresql://user:pass@host/db") == (
        "postgresql+asyncpg://user:pass@host/db"
    )
    assert to_adk_session_database_url("postgresql+psycopg://user:pass@host/db") == (
        "postgresql+asyncpg://user:pass@host/db"
    )


def test_unsupported_database_url_raises():
    with pytest.raises(ValueError):
        to_sync_database_url("mysql://user:pass@host/db")
