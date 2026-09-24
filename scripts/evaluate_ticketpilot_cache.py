"""Replay the held-out retrieval questions with and without the production cache."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import random
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ticketpilot.observability import RunTelemetry, current_run  # noqa: E402
from ticketpilot.retrieval import build_policy_retriever  # noqa: E402
from ticketpilot.retrieval_cache import CachedPolicyRetriever, create_cache_client  # noqa: E402
from ticketpilot.schemas import RequestPrincipal  # noqa: E402


def summary(rows):
    if not rows:
        return {"requests": 0}
    values = sorted(row["latency_ms"] for row in rows)
    return {
        "requests": len(rows),
        "errors": dict(Counter(row["error"] for row in rows if row["error"])),
        "mean_ms": round(statistics.mean(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": values[math.ceil(len(values) * 0.95) - 1],
        "max_ms": values[-1],
    }


async def measure(retriever, principal, query):
    telemetry = RunTelemetry("cache-benchmark")
    token = current_run.set(telemetry)
    started = perf_counter()
    row = {"error": None, "citations": None}
    try:
        async with asyncio.timeout(3):
            result = await retriever.search(principal, query)
        row["citations"] = [c.model_dump(mode="json") for c in result]
    except Exception as exc:
        row["error"] = type(exc).__name__
    finally:
        row["latency_ms"] = round((perf_counter() - started) * 1000, 3)
        current_run.reset(token)
    if telemetry.events:
        row["cache_status"] = telemetry.events[-1].get("cache_status")
        row["cache_write_status"] = telemetry.events[-1].get("cache_write_status")
    return row


async def run(args):
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = [case for case in payload["cases"] if case["split"] == "test"]
    run_id = uuid4().hex
    touched_keys = set()

    class IsolatedCache(CachedPolicyRetriever):
        def cache_key(self, *values):
            key = f"ticketpilot:cache-benchmark:{run_id}:" + super().cache_key(*values)
            touched_keys.add(key)
            return key

    started = perf_counter()
    backend = build_policy_retriever(args.manifest, "rerank", args.model_cache)
    rng = random.Random(args.seed)
    sequence = [(case, repetition) for repetition in range(args.repeats) for case in cases]
    rng.shuffle(sequence)
    report = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "dataset_id": payload["dataset_id"],
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "split": "test",
        "distinct_cases": len(cases),
        "answerable_cases": sum(bool(c["relevant_ids"]) for c in cases),
        "repeats_per_case": args.repeats,
        "pairs": len(sequence),
        "seed": args.seed,
        "metadata": backend.metadata(),
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
        "model_load_seconds_excluded": round(perf_counter() - started, 3),
        "method": (
            "Serial paired replay of the existing held-out test split; shuffled repeated queries; "
            "alternate randomized uncached/cache arm order per pair; warmed BGE index; "
            "Redis initially empty in isolated namespace; default TTL 600/30s with jitter; "
            "3s call timeout; includes misses/negative results/errors; no LLM/HTTP/SQL. "
            "Designed repetition is not a real traffic distribution."
        ),
        "rows": [],
    }
    async with create_cache_client(os.environ["TICKETPILOT_REDIS_URL"], 0.15) as client:
        cache = IsolatedCache(backend, client)
        try:
            for index, (case, repetition) in enumerate(sequence):
                principal = RequestPrincipal(
                    tenant_id=case["tenant_id"], actor_id="cache-evaluation", role="CUSTOMER"
                )
                arms = [("uncached", backend), ("cached", cache)]
                rng.shuffle(arms)
                pair = {
                    "index": index,
                    "case_id": case["id"],
                    "repetition": repetition,
                    "arm_order": [name for name, _ in arms],
                }
                for name, retriever in arms:
                    pair[name] = await measure(retriever, principal, case["query"])
                pair["same_citations"] = (
                    not pair["uncached"]["error"]
                    and not pair["cached"]["error"]
                    and pair["uncached"]["citations"] == pair["cached"]["citations"]
                )
                report["rows"].append(pair)
                if (index + 1) % 25 == 0:
                    print(
                        json.dumps({"completed_pairs": index + 1, "total": len(sequence)}),
                        flush=True,
                    )
        finally:
            if touched_keys:
                await client.delete(*touched_keys, *(key + ":lease" for key in touched_keys))
    uncached = [pair["uncached"] for pair in report["rows"]]
    cached = [pair["cached"] for pair in report["rows"]]
    report["summary"] = {
        "uncached": summary(uncached),
        "cached_all": summary(cached),
        "cached_hits": summary([r for r in cached if r.get("cache_status") == "HIT"]),
        "cached_misses": summary([r for r in cached if r.get("cache_status") != "HIT"]),
        "cache_status_counts": dict(Counter(r.get("cache_status", "UNKNOWN") for r in cached)),
        "same_citation_pairs": sum(pair["same_citations"] for pair in report["rows"]),
    }
    report["passed"] = len(report["rows"]) == len(sequence) and all(
        pair["same_citations"] for pair in report["rows"]
    )
    report["finished_at"] = datetime.now(UTC).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"passed": report["passed"], "summary": report["summary"]}), flush=True)
    return report["passed"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "data/ticketpilot/evals/retrieval_v1.json"
    )
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "data/ticketpilot/policy_v2_manifest.json"
    )
    parser.add_argument("--model-cache", default=os.environ.get("TICKETPILOT_MODEL_CACHE"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260916)
    args = parser.parse_args()
    if args.output.exists() or not 1 <= args.repeats <= 20:
        parser.error("output must not exist and repeats must be between 1 and 20")
    if not asyncio.run(run(args)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
