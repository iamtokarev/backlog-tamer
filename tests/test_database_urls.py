from __future__ import annotations

import pytest

from backlog_tamer.database_urls import to_sync_database_url, uses_external_pooler


def test_sqlite_database_urls_are_converted_for_the_sync_driver():
    assert to_sync_database_url("sqlite+aiosqlite:///local.db") == "sqlite:///local.db"
    assert to_sync_database_url("sqlite:///local.db") == "sqlite:///local.db"


def test_postgres_database_urls_are_converted_for_the_sync_driver():
    assert to_sync_database_url("postgresql://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert to_sync_database_url("postgresql+asyncpg://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )


def test_unsupported_database_url_raises():
    with pytest.raises(ValueError):
        to_sync_database_url("mysql://user:pass@host/db")


def test_only_the_supabase_pooler_counts_as_an_external_pooler():
    assert uses_external_pooler(
        "postgresql://user:pw@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"
    )
    assert not uses_external_pooler("postgresql://user:pw@db.example.com:5432/postgres")
