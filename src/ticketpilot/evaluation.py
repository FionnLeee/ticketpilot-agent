import asyncio
import hashlib
import math
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.runnables import RunnableConfig
from pydantic import Field, model_validator

from ticketpilot.schemas import TicketClassification, TicketPilotModel

if TYPE_CHECKING:
    from ticketpilot.reasoning import TicketReasoner

SCORED_FIELDS = (
    "category",
    "order_reference",
    "requested_refund_amount",
    "full_refund_requested",
)


class EvaluationCase(TicketPilotModel):
    case_id: str = Field(min_length=1)
    scenario_family: str = Field(min_length=1)
    message: str = Field(min_length=1)
    known_order_reference: str | None = None
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    expected: TicketClassification


class EvaluationDataset(TicketPilotModel):
    dataset_id: str
    split: Literal["development", "evaluation"]
    provenance: str
    label_policy: str
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> "EvaluationDataset":
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case_id must be unique")
        return self


def load_dataset(path: Path) -> tuple[EvaluationDataset, str]:
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return EvaluationDataset.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def score(expected: TicketClassification, actual: TicketClassification) -> dict[str, bool]:
    matches = {}
    for field in SCORED_FIELDS:
        left, right = getattr(expected, field), getattr(actual, field)
        if field == "order_reference":
            left = left.upper() if left else None
            right = right.upper() if right else None
        matches[field] = left == right
    return matches


async def evaluate(
    dataset: EvaluationDataset,
    reasoner: "TicketReasoner",
    config: RunnableConfig,
    *,
    metadata: dict[str, Any],
    limit: int | None = None,
    timeout_seconds: float = 60,
    concurrency: int = 1,
) -> dict[str, Any]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    started_at = datetime.now(UTC).isoformat()
    semaphore = asyncio.Semaphore(concurrency)

    async def run_case(case: EvaluationCase) -> dict[str, Any]:
        async with semaphore:
            return await classify_case(case)

    async def classify_case(case: EvaluationCase) -> dict[str, Any]:
        started = time.perf_counter()
        actual = None
        error = None
        matches = dict.fromkeys(SCORED_FIELDS, False)
        try:
            async with asyncio.timeout(timeout_seconds):
                actual = await reasoner.classify(case.message, case.known_order_reference, config)
                matches = score(case.expected, actual)
        except Exception as exc:
            error = type(exc).__name__
        return {
            "case_id": case.case_id,
            "scenario_family": case.scenario_family,
            "difficulty": case.difficulty,
            "message": case.message,
            "known_order_reference": case.known_order_reference,
            "expected": case.expected.model_dump(mode="json", include=set(SCORED_FIELDS)),
            "actual": actual.model_dump(mode="json") if actual is not None else None,
            "field_matches": matches,
            "exact_match": all(matches.values()),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error_type": error,
        }

    rows = await asyncio.gather(*(run_case(case) for case in dataset.cases[:limit]))
    count = len(rows)
    exact_matches = sum(row["exact_match"] for row in rows)
    accuracy = exact_matches / count
    z = 1.96
    denominator = 1 + z**2 / count
    center = (accuracy + z**2 / (2 * count)) / denominator
    margin = z * math.sqrt(accuracy * (1 - accuracy) / count + z**2 / (4 * count**2)) / denominator
    scenario_counts: dict[str, Counter] = {}
    difficulty_counts: dict[str, Counter] = {}
    categories = sorted(
        {str(row["expected"]["category"]) for row in rows}
        | {str(row["actual"]["category"]) for row in rows if row["actual"] is not None}
    )
    confusion = {expected: dict.fromkeys(categories, 0) for expected in categories}
    for row in rows:
        scenario_counts.setdefault(row["scenario_family"], Counter())["total"] += 1
        scenario_counts[row["scenario_family"]]["correct"] += int(row["exact_match"])
        difficulty_counts.setdefault(row["difficulty"], Counter())["total"] += 1
        difficulty_counts[row["difficulty"]]["correct"] += int(row["exact_match"])
        if row["actual"] is not None:
            confusion[str(row["expected"]["category"])][str(row["actual"]["category"])] += 1
    category_metrics = {}
    for category in categories:
        true_positive = confusion[category][category]
        false_positive = sum(
            confusion[other][category] for other in categories if other != category
        )
        false_negative = sum(
            confusion[category][other] for other in categories if other != category
        )
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0
        )
        category_metrics[category] = {
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
            "support": sum(confusion[category].values()),
        }
    latencies = sorted(row["latency_ms"] for row in rows)

    def grouped_accuracy(groups: dict[str, Counter]) -> dict[str, dict[str, float | int]]:
        return {
            key: {
                "correct": values["correct"],
                "total": values["total"],
                "accuracy": values["correct"] / values["total"],
            }
            for key, values in sorted(groups.items())
        }

    return {
        "dataset_id": dataset.dataset_id,
        "split": dataset.split,
        "started_at": started_at,
        "metadata": metadata,
        "scored_fields": SCORED_FIELDS,
        "case_count": count,
        "error_count": sum(row["error_type"] is not None for row in rows),
        "concurrency": concurrency,
        "exact_match_accuracy": accuracy,
        "exact_match_wilson_95": [max(0, center - margin), min(1, center + margin)],
        "field_accuracy": {
            field: sum(row["field_matches"][field] for row in rows) / count
            for field in SCORED_FIELDS
        },
        "scenario_accuracy": grouped_accuracy(scenario_counts),
        "difficulty_accuracy": grouped_accuracy(difficulty_counts),
        "category_metrics": category_metrics,
        "category_confusion_matrix": confusion,
        "latency_ms": {
            "p50": latencies[math.ceil(count * 0.50) - 1],
            "p95": latencies[math.ceil(count * 0.95) - 1],
            "p99": latencies[math.ceil(count * 0.99) - 1],
        },
        "cases": rows,
    }
