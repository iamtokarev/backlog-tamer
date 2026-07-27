from __future__ import annotations

import pytest
from sqlalchemy.pool import NullPool

from backlog_tamer.application.database_urls import (
    async_engine_options,
    to_adk_session_database_url,
    to_sync_database_url,
    uses_external_pooler,
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


def test_direct_connections_get_no_special_engine_options():
    assert (
        async_engine_options("postgresql://user:pw@db.example.com:5432/postgres") == {}
    )
    assert async_engine_options("sqlite:///backlog_tamer.db") == {}
    assert not uses_external_pooler("postgresql://user:pw@db.example.com:5432/postgres")


def test_transaction_pooler_disables_the_asyncpg_statement_cache():
    url = "postgresql://user:pw@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"

    options = async_engine_options(url)

    assert uses_external_pooler(url)
    # asyncpg prepares every statement and caches it per connection; behind
    # PgBouncer the next transaction lands on a backend that never saw it.
    assert options["connect_args"] == {"statement_cache_size": 0}
    assert options["poolclass"] is NullPool
