"""Bulk loader for the synthetic history dataset: COPY in FK order, idempotent reruns."""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, LiteralString, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from ticketpilot.db import BusinessConnection, BusinessPool
from ticketpilot.history_data import (
    APPROVAL_COLUMNS,
    EVENT_COLUMNS,
    MESSAGE_COLUMNS,
    ORDER_COLUMNS,
    TICKET_COLUMNS,
    HistoryBatch,
    HistoryConfig,
    generate_history,
)

TABLE_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("orders", ORDER_COLUMNS),
    ("tickets", TICKET_COLUMNS),
    ("approvals", APPROVAL_COLUMNS),
    ("ticket_messages", MESSAGE_COLUMNS),
    ("audit_events", EVENT_COLUMNS),
)
# Child tables first so deletes never violate a foreign key.
PURGE_ORDER = ("audit_events", "approvals", "ticket_messages", "tickets", "orders")

_COPY_ESCAPES = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"})


@dataclass
class LoadReport:
    dataset_id: str
    mode: str
    fingerprint: str
    row_counts: dict[str, int] = field(default_factory=dict)
    inserted_counts: dict[str, int] = field(default_factory=dict)
    table_seconds: dict[str, float] = field(default_factory=dict)
    generate_seconds: float = 0.0
    total_seconds: float = 0.0
    batches: int = 0


def _copy_value(value: Any) -> str:
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, str):
        return value.translate(_COPY_ESCAPES)
    if isinstance(value, datetime | Decimal | UUID | int):
        return str(value)
    raise TypeError(f"Unsupported COPY value type: {type(value)!r}")


def format_copy_text(rows: Iterable[tuple[Any, ...]]) -> str:
    return "".join("\t".join(_copy_value(value) for value in row) + "\n" for row in rows)


async def _copy_direct(
    connection: BusinessConnection, table: str, columns: tuple[str, ...], rows: list[tuple]
) -> int:
    column_list = ", ".join(columns)
    sql = cast(LiteralString, f"COPY ticketpilot.{table} ({column_list}) FROM STDIN")
    async with connection.cursor() as cursor, cursor.copy(sql) as copy:
        for start in range(0, len(rows), 5000):
            await copy.write(format_copy_text(rows[start : start + 5000]))
    return len(rows)


async def _copy_dedupe(
    connection: BusinessConnection, table: str, columns: tuple[str, ...], rows: list[tuple]
) -> int:
    """Stage through a temp table so a rerun skips rows that already exist."""
    staging = f"stage_{table}"
    column_list = ", ".join(columns)
    await connection.execute(
        cast(
            LiteralString,
            f"CREATE TEMP TABLE {staging} (LIKE ticketpilot.{table} INCLUDING DEFAULTS) "
            "ON COMMIT DROP",
        )
    )
    sql = cast(LiteralString, f"COPY {staging} ({column_list}) FROM STDIN")
    async with connection.cursor() as cursor, cursor.copy(sql) as copy:
        for start in range(0, len(rows), 5000):
            await copy.write(format_copy_text(rows[start : start + 5000]))
    cursor = await connection.execute(
        cast(
            LiteralString,
            f"INSERT INTO ticketpilot.{table} ({column_list}) "
            f"SELECT {column_list} FROM {staging} ON CONFLICT DO NOTHING",
        )
    )
    return cursor.rowcount


async def count_history_rows(pool: BusinessPool, config: HistoryConfig) -> dict[str, int]:
    tenants = config.tenant_ids()
    counts: dict[str, int] = {}
    async with pool.connection() as connection:
        for table, _ in TABLE_COLUMNS:
            cursor = await connection.execute(
                cast(
                    LiteralString,
                    f"SELECT count(*) AS n FROM ticketpilot.{table} WHERE tenant_id = ANY(%s)",
                ),
                (tenants,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError(f"Count query returned no row for {table}")
            counts[table] = row["n"]
    return counts


async def get_registered_dataset(pool: BusinessPool, dataset_id: str) -> dict[str, Any] | None:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT dataset_id, fingerprint, manifest, row_counts, loaded_at
            FROM ticketpilot.synthetic_datasets
            WHERE dataset_id = %s
            """,
            (dataset_id,),
        )
        return await cursor.fetchone()


async def purge_history(pool: BusinessPool, config: HistoryConfig) -> dict[str, int]:
    """Delete everything in the dataset's tenant namespace, including live runs on it."""
    tenants = config.tenant_ids()
    deleted: dict[str, int] = {}
    async with pool.connection() as connection, connection.transaction():
        for table in PURGE_ORDER:
            cursor = await connection.execute(
                cast(LiteralString, f"DELETE FROM ticketpilot.{table} WHERE tenant_id = ANY(%s)"),
                (tenants,),
            )
            deleted[table] = cursor.rowcount
        await connection.execute(
            "DELETE FROM ticketpilot.synthetic_datasets WHERE dataset_id = %s",
            (config.dataset_id,),
        )
    return deleted


async def load_history(
    pool: BusinessPool,
    config: HistoryConfig,
    *,
    batch_size: int = 20_000,
    replace: bool = False,
    progress: Callable[[str], None] | None = None,
) -> LoadReport:
    """Load the dataset; already-registered datasets are skipped, partial loads resumed."""
    fingerprint = config.fingerprint()
    report = LoadReport(dataset_id=config.dataset_id, mode="direct", fingerprint=fingerprint)
    started = time.perf_counter()
    registered = await get_registered_dataset(pool, config.dataset_id)
    if registered is not None and not replace:
        if registered["fingerprint"] != fingerprint:
            raise RuntimeError(
                f"Dataset {config.dataset_id} is registered with a different fingerprint; "
                "use replace to rebuild it"
            )
        report.mode = "skipped"
        report.row_counts = await count_history_rows(pool, config)
        report.total_seconds = time.perf_counter() - started
        return report
    if replace:
        await purge_history(pool, config)
        report.mode = "replace"
    existing = await count_history_rows(pool, config)
    if any(existing.values()):
        report.mode = "resume"
    copy_rows = _copy_dedupe if report.mode == "resume" else _copy_direct

    batches = generate_history(config, batch_size)
    while True:
        generate_started = time.perf_counter()
        batch = next(batches, None)
        report.generate_seconds = round(
            report.generate_seconds + time.perf_counter() - generate_started, 3
        )
        if batch is None:
            break
        report.batches += 1
        async with pool.connection() as connection, connection.transaction():
            await connection.execute("SET LOCAL synchronous_commit = off")
            for table, columns in TABLE_COLUMNS:
                rows = batch_rows(batch, table)
                table_started = time.perf_counter()
                inserted = await copy_rows(connection, table, columns, rows)
                report.table_seconds[table] = round(
                    report.table_seconds.get(table, 0.0) + time.perf_counter() - table_started, 3
                )
                report.row_counts[table] = report.row_counts.get(table, 0) + len(rows)
                report.inserted_counts[table] = report.inserted_counts.get(table, 0) + inserted
        if progress is not None:
            progress(
                f"batch {report.batches}: orders={report.row_counts['orders']} "
                f"tickets={report.row_counts['tickets']} events={report.row_counts['audit_events']}"
            )

    async with pool.connection() as connection:
        await connection.execute(
            "ANALYZE ticketpilot.orders, ticketpilot.tickets, ticketpilot.approvals, "
            "ticketpilot.ticket_messages, ticketpilot.audit_events"
        )
    actual = await count_history_rows(pool, config)
    if actual != report.row_counts:
        raise RuntimeError(
            f"Loaded counts {actual} do not match generated counts {report.row_counts}"
        )
    async with pool.connection() as connection, connection.transaction():
        await connection.execute(
            """
            INSERT INTO ticketpilot.synthetic_datasets (dataset_id, fingerprint, manifest, row_counts)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (dataset_id) DO UPDATE SET
                fingerprint = EXCLUDED.fingerprint,
                manifest = EXCLUDED.manifest,
                row_counts = EXCLUDED.row_counts,
                loaded_at = now()
            """,
            (config.dataset_id, fingerprint, Jsonb(config.describe()), Jsonb(report.row_counts)),
        )
    report.total_seconds = round(time.perf_counter() - started, 3)
    return report


def batch_rows(batch: HistoryBatch, table: str) -> list[tuple[Any, ...]]:
    attribute = {"ticket_messages": "messages", "audit_events": "events"}.get(table, table)
    return getattr(batch, attribute)
