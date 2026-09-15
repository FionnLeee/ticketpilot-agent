import argparse
import asyncio
import json
import math
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def parse_levels(value: str) -> tuple[int, ...]:
    try:
        levels = tuple(int(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("levels must be comma-separated integers") from exc
    if not levels or any(level < 1 or level > 1000 for level in levels):
        raise argparse.ArgumentTypeError("levels must be between 1 and 1000")
    return levels


def percentile(values: list[float], quantile: float) -> float:
    return values[math.ceil(len(values) * quantile) - 1]


async def run(args: argparse.Namespace) -> dict:
    token = os.environ.get("TICKETPILOT_BENCHMARK_TOKEN")
    if not token:
        raise ValueError("Set TICKETPILOT_BENCHMARK_TOKEN to a tenant-scoped demo token")
    headers = {"Authorization": f"Bearer {token}"}
    limits = httpx.Limits(
        max_connections=max(args.levels),
        max_keepalive_connections=max(args.levels),
    )
    report = {
        "experiment_id": f"http-read-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        "started_at": datetime.now(UTC).isoformat(),
        "code_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "httpx": httpx.__version__,
        },
        "method": (
            "HTTP GET through Uvicorn/FastAPI, TicketPilot bearer authentication and "
            "PostgreSQL repository; one existing tenant-owned ticket; no LLM or write action."
        ),
        "base_url": args.base_url,
        "phases": [],
    }
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(
        base_url=args.base_url,
        headers=headers,
        limits=limits,
        timeout=timeout,
    ) as client:
        response = await client.get("/v1/tickets", params={"limit": 1})
        response.raise_for_status()
        items = response.json()["items"]
        if not items:
            raise RuntimeError("The benchmark principal has no readable ticket")
        ticket_id = items[0]["id"]
        report["target_ticket_id"] = ticket_id

        for _ in range(args.warmup):
            warmup = await client.get(f"/v1/tickets/{ticket_id}")
            warmup.raise_for_status()

        for concurrency in args.levels:
            semaphore = asyncio.Semaphore(concurrency)
            rows: list[dict[str, float | int]] = []

            async def one(index: int) -> None:
                async with semaphore:
                    started = time.perf_counter()
                    response = await client.get(f"/v1/tickets/{ticket_id}")
                    rows.append(
                        {
                            "index": index,
                            "status": response.status_code,
                            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        }
                    )

            started = time.perf_counter()
            await asyncio.gather(*(one(index) for index in range(args.requests)))
            elapsed = time.perf_counter() - started
            latencies = sorted(float(row["latency_ms"]) for row in rows)
            status_counts = Counter(int(row["status"]) for row in rows)
            phase = {
                "concurrency": concurrency,
                "requests": args.requests,
                "elapsed_seconds": round(elapsed, 3),
                "requests_per_second": round(args.requests / elapsed, 2),
                "status_counts": {str(code): count for code, count in status_counts.items()},
                "p50_ms": percentile(latencies, 0.50),
                "p95_ms": percentile(latencies, 0.95),
                "p99_ms": percentile(latencies, 0.99),
                "client_semaphore_wait_excluded_from_latency": True,
            }
            report["phases"].append(phase)
            print(json.dumps(phase), flush=True)
            if status_counts != Counter({200: args.requests}):
                report.setdefault("failures", []).append(f"concurrency-{concurrency}")

    report["passed"] = not report.get("failures")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--levels", type=parse_levels, default=(1, 20, 50, 100, 200))
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if not 100 <= args.requests <= 10000:
        parser.error("--requests must be between 100 and 10000")
    if not 0 <= args.warmup <= 1000:
        parser.error("--warmup must be between 0 and 1000")
    if args.output.exists():
        parser.error("--output already exists")
    report = asyncio.run(run(args))
    print(json.dumps({"passed": report["passed"], "output": str(args.output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
