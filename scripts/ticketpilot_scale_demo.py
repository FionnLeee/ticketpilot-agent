import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from ticketpilot.db import apply_migrations  # noqa: E402
from ticketpilot.errors import TicketPilotError  # noqa: E402
from ticketpilot.graph import build_ticketpilot_graph  # noqa: E402
from ticketpilot.orders import PostgresOrderRepository  # noqa: E402
from ticketpilot.policies import LocalPolicyRetriever, load_policy_corpus  # noqa: E402
from ticketpilot.reasoning import DeterministicDemoReasoner  # noqa: E402
from ticketpilot.repositories import TicketRepository  # noqa: E402
from ticketpilot.scale_data import (  # noqa: E402
    describe_orders,
    generate_scale_orders,
    insert_scale_orders,
)
from ticketpilot.schemas import (  # noqa: E402
    ApprovalDecisionRequest,
    CreateTicketRequest,
    RequestPrincipal,
)
from ticketpilot.services import TicketService  # noqa: E402
from ticketpilot.workflow_repository import TicketWorkflowRepository  # noqa: E402


class DelayedDemoReasoner(DeterministicDemoReasoner):
    async def classify(self, message, known_order_reference, config):
        await asyncio.sleep(0.02)
        return await super().classify(message, known_order_reference, config)


async def run(args):
    orders = generate_scale_orders()
    summary = describe_orders(orders)
    summary["orders_sha256"] = hashlib.sha256(
        json.dumps([asdict(o) for o in orders], default=str, sort_keys=True).encode()
    ).hexdigest()
    corpus = load_policy_corpus(ROOT / "data/ticketpilot/scale_policy_manifest.json")
    summary["policy_chunks"] = len(corpus.chunks)
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False))
        return
    if not args.output or args.output.exists():
        raise ValueError("Provide a new --output path")
    dsn = os.environ["TICKETPILOT_SCALE_DSN"]
    run_id = str(uuid4())
    report = {
        "dataset": summary,
        "experiment_id": run_id,
        "code_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        ),
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
        "method": "In-process TicketService + LangGraph + real PostgreSQL business/checkpoints; no HTTP, no LLM, no real payment. 20ms synthetic classify delay.",
        "pool_max_each": 10,
        "phases": [],
    }
    async with (
        AsyncConnectionPool(dsn, min_size=2, max_size=10, kwargs={"row_factory": dict_row}) as pool,
        AsyncConnectionPool(
            dsn, min_size=2, max_size=10, kwargs={"row_factory": dict_row, "autocommit": True}
        ) as saver_pool,
    ):
        await apply_migrations(pool)
        await insert_scale_orders(pool, orders)
        saver = AsyncPostgresSaver(saver_pool)
        await saver.setup()
        service = TicketService(
            TicketRepository(pool),
            workflow=build_ticketpilot_graph(saver),
            workflow_repository=TicketWorkflowRepository(pool),
            order_reader=PostgresOrderRepository(pool),
            policy_retriever=LocalPolicyRetriever(corpus),
            reasoner=DelayedDemoReasoner(),
        )
        scenario_specs = [
            ("物流", "查询物流", True, "ANSWERED", False),
            ("政策", "退款规则是什么", False, "ANSWERED", False),
            ("缺金额", "我要退款", True, "NEEDS_INPUT", False),
            ("缺订单", "我要退款0.01元", False, "NEEDS_INPUT", False),
            ("全额", "我要全额退款", True, "WAITING_APPROVAL", True),
            ("明确金额", "我要退款0.01元", True, "WAITING_APPROVAL", True),
            ("否定退款", "不要退款，只查物流", True, "ANSWERED", False),
            ("取消退款", "取消退款申请", True, "ANSWERED", False),
            ("订单不存在", "查询 O-SCALE-NOTFOUND 的物流", False, "NEEDS_INPUT", False),
            ("无政策", "火星居住保障政策", False, "INSUFFICIENT_EVIDENCE", False),
            ("超额", "我要退款999999元", True, "ANSWERED", False),
            ("零余额", "我要退款0.01元", "zero", "ANSWERED", False),
        ]
        report["scenario_checks"] = []
        refund_target = None
        for tenant_index in range(10):
            for name, message, link, expected, approval in scenario_specs:
                order = orders[tenant_index * 1000 + (9 if link == "zero" else 2)]
                principal = RequestPrincipal(
                    tenant_id=order.tenant_id, actor_id=order.customer_id, role="CUSTOMER"
                )
                reference = (
                    order.order_reference
                    if link is True or link == "zero"
                    else (link if isinstance(link, str) else None)
                )
                result = await service.create_ticket(
                    principal,
                    CreateTicketRequest(
                        subject=f"合成覆盖-{name}", message=message, order_reference=reference
                    ),
                    f"{run_id}-coverage-{tenant_index}-{name}",
                )
                passed = (
                    result.ticket.processing_result == expected
                    and (result.pending_approval is not None) == approval
                )
                report["scenario_checks"].append(
                    {
                        "tenant": order.tenant_id,
                        "scenario": name,
                        "expected": expected,
                        "actual": str(result.ticket.processing_result),
                        "passed": passed,
                        "ticket_id": str(result.ticket.id),
                    }
                )
                if not passed:
                    report.setdefault("failures", []).append(f"coverage-{tenant_index}-{name}")
                if tenant_index == 0 and name == "明确金额":
                    refund_target = (order, result.pending_approval)
        print("Scenario coverage: 120 workflow cases completed", flush=True)
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS n FROM ticketpilot.orders WHERE tenant_id LIKE 'tenant-scale-%%'"
            )
            report["database_order_count"] = (await cursor.fetchone())["n"]
        if report["database_order_count"] != 10000:
            raise AssertionError("Scale database must contain exactly 10000 scale orders")

        async def phase(name, concurrency, count, same_key=False):
            semaphore = asyncio.Semaphore(concurrency)
            rows = []

            async def one(index):
                async with semaphore:
                    order = orders[2 if same_key else (index * 97) % len(orders)]
                    principal = RequestPrincipal(
                        tenant_id=order.tenant_id, actor_id=order.customer_id, role="CUSTOMER"
                    )
                    payload = CreateTicketRequest(
                        subject="合成规模验证",
                        message=f"查询订单 {order.order_reference} 的物流",
                        order_reference=order.order_reference,
                    )
                    started = time.perf_counter()
                    row = {"index": index}
                    try:
                        async with asyncio.timeout(60):
                            result = await service.create_ticket(
                                principal,
                                payload,
                                f"{run_id}-{name}-{'shared' if same_key else index}",
                            )
                        row.update(
                            status="OK",
                            ticket_id=str(result.ticket.id),
                            run_id=str(result.run_id),
                            processing_result=str(result.ticket.processing_result),
                        )
                    except TicketPilotError as exc:
                        row.update(status=exc.code)
                    except Exception as exc:
                        row.update(status=type(exc).__name__)
                    row["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
                    rows.append(row)

            started = time.perf_counter()
            await asyncio.gather(*(one(index) for index in range(count)))
            elapsed = time.perf_counter() - started
            latencies = sorted(row["latency_ms"] for row in rows)
            from collections import Counter

            result = {
                "name": name,
                "concurrency": concurrency,
                "requests": count,
                "elapsed_seconds": round(elapsed, 3),
                "attempts_per_second": round(count / elapsed, 2),
                "status_counts": dict(Counter(row["status"] for row in rows)),
                "latency_excludes_client_semaphore_wait": True,
                "p50_ms": latencies[math.ceil(count * 0.50) - 1],
                "p95_ms": latencies[math.ceil(count * 0.95) - 1],
                "p99_ms": latencies[math.ceil(count * 0.99) - 1],
                "rows": sorted(rows, key=lambda row: row["index"]),
            }
            report["phases"].append(result)
            print(json.dumps({k: v for k, v in result.items() if k != "rows"}), flush=True)
            return rows

        for concurrency in (1, 10, 30):
            rows = await phase(f"independent-{concurrency}", concurrency, args.requests)
            if any(row["status"] != "OK" or row["processing_result"] != "ANSWERED" for row in rows):
                report.setdefault("failures", []).append(f"independent-{concurrency}")
        rows = await phase("same-request", 30, 30, same_key=True)
        ids = {row["ticket_id"] for row in rows if row["status"] == "OK"}
        runs = {row["run_id"] for row in rows if row["status"] == "OK"}
        async with pool.connection() as connection:
            cursor = await connection.execute(
                """SELECT count(*) AS n FROM ticketpilot.tickets
                   WHERE tenant_id = %s AND create_idempotency_key = %s""",
                (orders[2].tenant_id, f"{run_id}-same-request-shared"),
            )
            ticket_count = (await cursor.fetchone())["n"]
        report["same_request_invariant"] = {
            "distinct_returned_tickets": len(ids),
            "distinct_returned_runs": len(runs),
            "queried_ticket_count": ticket_count,
        }
        if (
            len(ids) != 1
            or len(runs) != 1
            or ticket_count != 1
            or any(row["status"] not in {"OK", "STATE_CONFLICT"} for row in rows)
        ):
            report.setdefault("failures", []).append("same-request")
        order, approval = refund_target
        approver = RequestPrincipal(
            tenant_id=order.tenant_id, actor_id="scale-approver", role="APPROVER"
        )
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT refundable_amount FROM ticketpilot.orders WHERE id = %s", (order.id,)
            )
            before = (await cursor.fetchone())["refundable_amount"]

        async def approve():
            try:
                async with asyncio.timeout(60):
                    await service.decide_approval(
                        approver,
                        approval.id,
                        ApprovalDecisionRequest(decision="APPROVE", reason="合成并发审批验证"),
                    )
                return "OK"
            except TicketPilotError as exc:
                return exc.code
            except Exception as exc:
                return type(exc).__name__

        statuses = await asyncio.gather(*(approve() for _ in range(30)))
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT refundable_amount FROM ticketpilot.orders WHERE id = %s", (order.id,)
            )
            after = (await cursor.fetchone())["refundable_amount"]
            cursor = await connection.execute(
                """SELECT count(*) AS n FROM ticketpilot.audit_events
                   WHERE approval_id = %s AND event_type = 'REFUND_EXECUTED'""",
                (approval.id,),
            )
            executions = (await cursor.fetchone())["n"]
        report["concurrent_approval"] = {
            "requests": 30,
            "status_counts": {s: statuses.count(s) for s in set(statuses)},
            "before": str(before),
            "after": str(after),
            "executions": executions,
        }
        from decimal import Decimal

        if (
            before - after != Decimal("0.01")
            or executions != 1
            or any(status not in {"OK", "STATE_CONFLICT"} for status in statuses)
        ):
            report.setdefault("failures", []).append("concurrent-approval")
        async with pool.connection() as connection:
            cursor = await connection.execute(
                """SELECT count(*) AS n FROM ticketpilot.audit_events
                   WHERE tenant_id LIKE 'tenant-scale-%%'"""
            )
            report["scale_audit_events_total"] = (await cursor.fetchone())["n"]
    report["passed"] = not report.get("failures")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": report["passed"], "output": str(args.output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--requests", type=int, default=100)
    arguments = parser.parse_args()
    if not 30 <= arguments.requests <= 1000:
        parser.error("--requests must be between 30 and 1000")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run(arguments))
