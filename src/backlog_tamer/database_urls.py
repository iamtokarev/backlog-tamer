from __future__ import annotations

from sqlalchemy.pool import NullPool


def to_sync_database_url(database_url: str) -> str:
    if database_url.startswith("sqlite+aiosqlite:///"):
        return database_url.replace("sqlite+aiosqlite:///", "sqlite:///", 1)
    if database_url.startswith("sqlite:///"):
        return database_url
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql+psycopg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    raise ValueError(f"Unsupported sync database URL: {database_url}")


def to_adk_session_database_url(database_url: str) -> str:
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if database_url.startswith("sqlite+aiosqlite:///"):
        return database_url
    if database_url.startswith("postgresql+psycopg://"):
        return database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    raise ValueError(f"Unsupported ADK session database URL: {database_url}")


def uses_external_pooler(database_url: str) -> bool:
    """True when connections go through a transaction-mode pooler.

    Supabase's pooler on port 6543 is PgBouncer in transaction mode: each
    transaction can land on a different backend, so nothing connection-scoped
    survives between them.
    """
    return "pooler.supabase.com" in database_url


def async_engine_options(database_url: str) -> dict[str, object]:
    """Engine kwargs that make asyncpg survive a transaction-mode pooler.

    asyncpg implicitly prepares every statement and caches it per connection.
    Behind PgBouncer the next transaction gets a different backend, where that
    statement was never prepared, and the query fails with
    'prepared statement "__asyncpg_stmt_N__" does not exist'. Turning
    asyncpg's own cache off is what fixes it; SQLAlchemy's
    prepared_statement_cache_size alone is not enough.
    """
    if not uses_external_pooler(database_url):
        return {}
    return {
        "poolclass": NullPool,
        "connect_args": {"statement_cache_size": 0},
    }
