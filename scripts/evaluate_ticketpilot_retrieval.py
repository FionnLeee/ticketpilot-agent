"""Ablate retrieval against a frozen, author-labelled diagnostic dataset."""

import argparse
import hashlib
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ticketpilot.policies import load_policy_corpus  # noqa: E402
from ticketpilot.retrieval import (  # noqa: E402
    FastEmbedBackend,
    FastEmbedReranker,
    PolicySearchIndex,
)
from ticketpilot.schemas import RequestPrincipal  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir")
    parser.add_argument("--min-similarity", type=float, default=0.50)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists")
    dataset_path = ROOT / "data/ticketpilot/evals/retrieval_v1.json"
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = [c for c in payload["cases"] if c["split"] == args.split]
    started = perf_counter()
    index = PolicySearchIndex(
        load_policy_corpus(ROOT / "data/ticketpilot/policy_v2_manifest.json"),
        FastEmbedBackend(args.cache_dir),
        FastEmbedReranker(args.cache_dir),
    )
    index.warm()
    report = {
        "dataset_id": payload["dataset_id"],
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "split": args.split,
        "note": payload["note"],
        "measured_at": datetime.now(UTC).isoformat(),
        "corpus_fingerprint": index.fingerprint,
        "corpus_chunks": len(index.corpus.chunks),
        "models": [index.embedding.model_name, index.reranker.model_name],
        "cold_start_seconds": round(perf_counter() - started, 3),
        "parameters": {
            "min_similarity": args.min_similarity,
            "k": 5,
            "candidate_limit": 12,
            "rrf_k": 60,
        },
        "strategies": {},
    }
    for strategy in ["keyword", "bm25", "dense", "hybrid", "rerank"]:
        results = []
        for case in cases:
            principal = RequestPrincipal(
                tenant_id=case["tenant_id"], actor_id="eval", role="CUSTOMER"
            )
            started = perf_counter()
            evidence = index.search(
                principal,
                case["query"],
                strategy=strategy,
                limit=5,
                as_of=datetime(2026, 9, 15, tzinfo=UTC),
                min_similarity=args.min_similarity,
            )
            ids = [e.chunk_id for e in evidence]
            rank = next(
                (i + 1 for i, identifier in enumerate(ids) if identifier in case["relevant_ids"]), 0
            )
            results.append(
                {
                    "case_id": case["id"],
                    "answerable": bool(case["relevant_ids"]),
                    "retrieved_ids": ids,
                    "rank": rank,
                    "latency_ms": round((perf_counter() - started) * 1000, 2),
                }
            )
        positive = [r for r in results if r["answerable"]]
        negative = [r for r in results if not r["answerable"]]
        summary = {
            "answerable_count": len(positive),
            "no_answer_count": len(negative),
            "recall_at_5": sum(r["rank"] > 0 for r in positive) / len(positive),
            "mrr_at_5": sum(1 / r["rank"] if r["rank"] else 0 for r in positive) / len(positive),
            "ndcg_at_5": sum(1 / math.log2(r["rank"] + 1) if r["rank"] else 0 for r in positive)
            / len(positive),
            "no_answer_specificity": sum(not r["retrieved_ids"] for r in negative) / len(negative),
            "p50_ms": float(np.percentile([r["latency_ms"] for r in results], 50)),
            "p95_ms": float(np.percentile([r["latency_ms"] for r in results], 95)),
        }
        report["strategies"][strategy] = {"summary": summary, "cases": results}
        print(strategy, json.dumps(summary), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
