import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ticketpilot.evaluation import evaluate, load_dataset  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=REPO_ROOT / "data/ticketpilot/evals/classification_dev_v1.json",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--scenario", action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if not 1 <= args.concurrency <= 10:
        parser.error("--concurrency must be between 1 and 10")
    dataset, digest = load_dataset(args.dataset)
    if args.scenario:
        selected = [case for case in dataset.cases if case.scenario_family in args.scenario]
        if not selected:
            parser.error("--scenario did not match any cases")
        dataset = dataset.model_copy(update={"cases": selected})
    if args.dry_run:
        print(
            json.dumps(
                {
                    "dataset_id": dataset.dataset_id,
                    "sha256": digest,
                    "case_count": len(dataset.cases[: args.limit]),
                    "network_calls": 0,
                }
            )
        )
        return
    if args.output is None:
        parser.error("--output is required for a real-model run")
    if args.output.exists():
        parser.error("--output already exists; choose a new filename")
    from core import get_model, settings
    from ticketpilot.reasoning import LangChainTicketReasoner

    if settings.USE_FAKE_MODEL or settings.DEFAULT_MODEL is None:
        parser.error("Configure a real DEFAULT_MODEL and USE_FAKE_MODEL=false")
    model = get_model(settings.DEFAULT_MODEL)
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO_ROOT, text=True).strip()
    )
    report = asyncio.run(
        evaluate(
            dataset,
            LangChainTicketReasoner(),
            {"configurable": {"model": settings.DEFAULT_MODEL}},
            limit=args.limit,
            concurrency=args.concurrency,
            metadata={
                "dataset_sha256": digest,
                "code_revision": revision,
                "working_tree_dirty": dirty,
                "reasoning_sha256": hashlib.sha256(
                    (REPO_ROOT / "src/ticketpilot/reasoning.py").read_bytes()
                ).hexdigest(),
                "model_route": str(settings.DEFAULT_MODEL),
                "model_name": getattr(model, "model_name", None) or getattr(model, "model", None),
                "temperature": getattr(model, "temperature", None),
                "timeout_seconds": 60,
                "request_concurrency": args.concurrency,
                "selected_scenarios": args.scenario,
                "note": "Bounded-concurrency evaluation; SDK retries may occur. No cost estimate.",
            },
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}))
    if report["error_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
