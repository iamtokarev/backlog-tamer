from __future__ import annotations


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


def uses_external_pooler(database_url: str) -> bool:
    """True when connections go through a transaction-mode pooler.

    Supabase's pooler on port 6543 is PgBouncer in transaction mode: each
    transaction can land on a different backend, so nothing connection-scoped
    survives between them.
    """
    return "pooler.supabase.com" in database_url
