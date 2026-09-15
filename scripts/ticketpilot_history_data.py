"""Generate, bulk-load, validate, and benchmark TicketPilot synthetic history."""

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from ticketpilot.db import BusinessConnection, apply_migrations  # noqa: E402
from ticketpilot.history_data import (  # noqa: E402
    DEFAULT_HISTORY_MANIFEST_PATH,
    HistoryConfig,
    load_history_config,
    summarize_history,
    with_order_count,
)
from ticketpilot.history_loader import load_history  # noqa: E402
from ticketpilot.history_quality import (  # noqa: E402
    run_quality_checks,
    snapshot_views_with_timings,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value)!r}")


def _git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _plan_nodes(plan: dict[str, Any]) -> list[str]:
    nodes = [str(plan["Node Type"])]
    for child in plan.get("Plans", []):
        nodes.extend(_plan_nodes(child))
    return nodes


async def collect_database_metrics(
    pool: AsyncConnectionPool[BusinessConnection], config: HistoryConfig
) -> dict[str, Any]:
    """Collect storage plus one real indexed access-path benchmark."""
    async with pool.connection() as connection:
        metadata_cursor = await connection.execute(
            """
            SELECT current_database() AS database_name,
                   current_setting('server_version') AS server_version,
                   pg_database_size(current_database()) AS database_size_bytes
            """
        )
        metadata = dict(await metadata_cursor.fetchone())
        table_cursor = await connection.execute(
            """
            SELECT relname AS table_name,
                   pg_relation_size(c.oid) AS heap_bytes,
                   pg_indexes_size(c.oid) AS index_bytes,
                   pg_total_relation_size(c.oid) AS total_bytes
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'ticketpilot'
              AND relname = ANY(%s)
            ORDER BY relname
            """,
            (["orders", "tickets", "ticket_messages", "approvals", "audit_events"],),
        )
        table_storage = [dict(row) for row in await table_cursor.fetchall()]
        sample_cursor = await connection.execute(
            """
            SELECT tenant_id, customer_id, count(*) AS ticket_count
            FROM ticketpilot.tickets
            WHERE tenant_id = ANY(%s)
            GROUP BY tenant_id, customer_id
            ORDER BY ticket_count DESC, tenant_id, customer_id
            LIMIT 1
            """,
            (config.tenant_ids(),),
        )
        sample = await sample_cursor.fetchone()
        if sample is None:
            return {**metadata, "table_storage": table_storage, "customer_lookup": None}

        explain_cursor = await connection.execute(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT id, status, processing_result, subject, updated_at
            FROM ticketpilot.tickets
            WHERE tenant_id = %s AND customer_id = %s
            ORDER BY updated_at DESC
            LIMIT 20
            """,
            (sample["tenant_id"], sample["customer_id"]),
        )
        explain = (await explain_cursor.fetchone())["QUERY PLAN"][0]
        plan = explain["Plan"]
        customer_lookup = {
            "tenant_id": sample["tenant_id"],
            "customer_id": sample["customer_id"],
            "matching_tickets": sample["ticket_count"],
            "execution_time_ms": explain["Execution Time"],
            "planning_time_ms": explain["Planning Time"],
            "plan_nodes": _plan_nodes(plan),
            "shared_hit_blocks": plan.get("Shared Hit Blocks", 0),
            "shared_read_blocks": plan.get("Shared Read Blocks", 0),
        }
    return {**metadata, "table_storage": table_storage, "customer_lookup": customer_lookup}


async def run_load(args: argparse.Namespace, config: HistoryConfig) -> dict[str, Any]:
    started = time.perf_counter()
    async with AsyncConnectionPool[BusinessConnection](
        args.dsn,
        min_size=1,
        max_size=4,
        kwargs={
            "autocommit": False,
            "row_factory": dict_row,
            "application_name": "ticketpilot-history-loader",
        },
        check=AsyncConnectionPool.check_connection,
    ) as pool:
        migrations = await apply_migrations(pool)
        load_report = await load_history(
            pool,
            config,
            batch_size=args.batch_size,
            replace=args.replace,
            progress=lambda line: print(line, file=sys.stderr, flush=True),
        )
        quality_started = time.perf_counter()
        quality_checks = await run_quality_checks(pool, config)
        quality_seconds = round(time.perf_counter() - quality_started, 3)
        failed = [check for check in quality_checks if not check["passed"]]
        if failed:
            names = ", ".join(check["name"] for check in failed)
            raise RuntimeError(f"Data-quality checks failed: {names}")
        analytics = await snapshot_views_with_timings(pool, config)
        database = await collect_database_metrics(pool, config)
    return {
        "report_schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "git_commit": _git_commit(),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "dataset": config.describe(),
        "fingerprint": config.fingerprint(),
        "migrations_applied": migrations,
        "load": asdict(load_report),
        "quality": {
            "passed": len(quality_checks),
            "failed": 0,
            "duration_seconds": quality_seconds,
            "checks": quality_checks,
        },
        "analytics": analytics,
        "database": database,
        "end_to_end_seconds": round(time.perf_counter() - started, 3),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_HISTORY_MANIFEST_PATH)
    parser.add_argument("--orders", type=int, help="isolated smaller variant for testing")
    parser.add_argument("--batch-size", type=int, default=20_000)
    parser.add_argument("--dsn", default=os.getenv("TICKETPILOT_HISTORY_DSN"))
    parser.add_argument("--replace", action="store_true", help="rebuild this dataset namespace")
    parser.add_argument("--dry-run", action="store_true", help="generate and count without PostgreSQL")
    parser.add_argument("--output", type=Path, help="optional JSON evidence report")
    parser.add_argument("--overwrite-output", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 100 or args.batch_size > 100_000:
        parser.error("--batch-size must be between 100 and 100000")
    if args.orders is not None and args.orders < 12:
        parser.error("--orders must be at least 12")
    if not args.dry_run and not args.dsn:
        parser.error("--dsn or TICKETPILOT_HISTORY_DSN is required unless --dry-run is used")
    if args.output and args.output.exists() and not args.overwrite_output:
        parser.error(f"output already exists: {args.output}; choose another path")
    return args


def main() -> None:
    args = parse_args()
    config = load_history_config(args.manifest.resolve())
    if args.orders is not None:
        config = with_order_count(config, args.orders)
    if args.dry_run:
        report = summarize_history(config, batch_size=args.batch_size)
    else:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        report = asyncio.run(run_load(args, config))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=_json_default)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"report: {args.output.resolve()}", file=sys.stderr)
    print(rendered)


if __name__ == "__main__":
    main()
