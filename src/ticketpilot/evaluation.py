import asyncio
import hashlib
import time
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
    expected: TicketClassification


class EvaluationDataset(TicketPilotModel):
    dataset_id: str
    split: Literal["development"]
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
) -> dict[str, Any]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    started_at = datetime.now(UTC).isoformat()
    rows: list[dict[str, Any]] = []
    for case in dataset.cases[:limit]:
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
        rows.append(
            {
                "case_id": case.case_id,
                "scenario_family": case.scenario_family,
                "message": case.message,
                "known_order_reference": case.known_order_reference,
                "expected": case.expected.model_dump(mode="json", include=set(SCORED_FIELDS)),
                "actual": actual.model_dump(mode="json") if actual is not None else None,
                "field_matches": matches,
                "exact_match": all(matches.values()),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "error_type": error,
            }
        )
    count = len(rows)
    return {
        "dataset_id": dataset.dataset_id,
        "split": dataset.split,
        "started_at": started_at,
        "metadata": metadata,
        "scored_fields": SCORED_FIELDS,
        "case_count": count,
        "error_count": sum(row["error_type"] is not None for row in rows),
        "exact_match_accuracy": sum(row["exact_match"] for row in rows) / count,
        "field_accuracy": {
            field: sum(row["field_matches"][field] for row in rows) / count
            for field in SCORED_FIELDS
        },
        "cases": rows,
    }
