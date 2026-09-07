import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, LiteralString, cast

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core.settings import settings
from memory.postgres import get_postgres_connection_string, validate_postgres_config
from ticketpilot.paths import find_ancestor_path


def find_migrations_dir(module_path: Path = Path(__file__)) -> Path:
    return find_ancestor_path(module_path, "migrations", "ticketpilot")


DEFAULT_MIGRATIONS_DIR = find_migrations_dir()
BusinessConnection = AsyncConnection[dict[str, Any]]
BusinessPool = AsyncConnectionPool[BusinessConnection]


@asynccontextmanager
async def get_ticketpilot_pool() -> AsyncIterator[BusinessPool]:
    validate_postgres_config()
    application_name = f"{settings.POSTGRES_APPLICATION_NAME}-ticketpilot"
    async with AsyncConnectionPool[BusinessConnection](
        get_postgres_connection_string(),
        min_size=settings.POSTGRES_MIN_CONNECTIONS_PER_POOL,
        max_size=settings.POSTGRES_MAX_CONNECTIONS_PER_POOL,
        kwargs={
            "autocommit": False,
            "row_factory": dict_row,
            "application_name": application_name,
        },
        check=AsyncConnectionPool.check_connection,
    ) as pool:
        yield pool


def migration_checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


async def apply_migrations(
    pool: BusinessPool, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> list[str]:
    migration_paths = sorted(migrations_dir.glob("*.sql"))
    if not migration_paths:
        raise FileNotFoundError(f"No TicketPilot migrations found in {migrations_dir}")

    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute("CREATE SCHEMA IF NOT EXISTS ticketpilot")
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticketpilot.schema_migrations (
                    version text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )

    applied: list[str] = []
    for migration_path in migration_paths:
        migration_sql = migration_path.read_text(encoding="utf-8")
        checksum = migration_checksum(migration_sql)
        version = migration_path.name

        async with pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('ticketpilot-schema-migrations'))"
                )
                cursor = await connection.execute(
                    "SELECT checksum FROM ticketpilot.schema_migrations WHERE version = %s",
                    (version,),
                )
                existing = await cursor.fetchone()
                if existing:
                    if existing["checksum"] != checksum:
                        raise RuntimeError(f"Applied migration {version} has a different checksum")
                    continue

                await connection.execute(cast(LiteralString, migration_sql))
                await connection.execute(
                    """
                    INSERT INTO ticketpilot.schema_migrations (version, checksum)
                    VALUES (%s, %s)
                    """,
                    (version, checksum),
                )
                applied.append(version)

    return applied
