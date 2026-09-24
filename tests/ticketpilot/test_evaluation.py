import asyncio
from pathlib import Path

import pytest

from ticketpilot.evaluation import EvaluationDataset, evaluate, load_dataset, score
from ticketpilot.schemas import TicketClassification

DATASET_PATH = (
    Path(__file__).resolve().parents[2] / "data/ticketpilot/evals/classification_dev_v1.json"
)


def test_dataset_contract_and_duplicate_ids():
    dataset, digest = load_dataset(DATASET_PATH)
    assert len(dataset.cases) == 18
    assert len(digest) == 64
    raw = dataset.model_dump()
    raw["cases"].append(raw["cases"][0])
    with pytest.raises(ValueError, match="unique"):
        EvaluationDataset.model_validate(raw)


def test_scoring_normalizes_money_and_order_but_does_not_hide_wrong_intent():
    expected = TicketClassification(
        category="REFUND", order_reference="TP-0005", requested_refund_amount="12.50"
    )
    actual = TicketClassification(
        category="REFUND",
        priority="HIGH",
        order_reference="tp-0005",
        requested_refund_amount="12.5",
    )
    assert all(score(expected, actual).values())
    actual.category = "OTHER"
    actual.requested_refund_amount = None
    matches = score(expected, actual)
    assert not matches["category"]
    assert not matches["requested_refund_amount"]


@pytest.mark.asyncio
async def test_failures_count_in_denominator_and_run_continues():
    dataset, _ = load_dataset(DATASET_PATH)

    class StubReasoner:
        calls = 0

        async def classify(self, message, known_order_reference, config):
            index = self.calls
            self.calls += 1
            if index == 1:
                raise RuntimeError("sensitive provider detail must not enter report")
            return dataset.cases[index].expected

    report = await evaluate(dataset, StubReasoner(), {}, metadata={}, limit=3)
    assert report["case_count"] == 3
    assert report["error_count"] == 1
    assert report["exact_match_accuracy"] == pytest.approx(2 / 3)
    assert report["cases"][1]["error_type"] == "RuntimeError"
    assert "sensitive" not in str(report)
    assert report["cases"][2]["exact_match"]


@pytest.mark.asyncio
async def test_timeout_and_invalid_limit():
    dataset, _ = load_dataset(DATASET_PATH)

    class SlowReasoner:
        async def classify(self, *args):
            await asyncio.sleep(1)

    report = await evaluate(
        dataset, SlowReasoner(), {}, metadata={}, limit=1, timeout_seconds=0.001
    )
    assert report["cases"][0]["error_type"] == "TimeoutError"
    assert report["exact_match_accuracy"] == 0
    with pytest.raises(ValueError, match="positive"):
        await evaluate(dataset, SlowReasoner(), {}, metadata={}, limit=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("compatible", [True, False])
async def test_reasoner_selects_compatible_transport_and_keeps_local_validation(
    monkeypatch, compatible
):
    from unittest.mock import AsyncMock, Mock

    from pydantic import ValidationError

    from ticketpilot import reasoning

    runnable = Mock()
    runnable.ainvoke = AsyncMock(
        return_value={"category": "REFUND", "requested_refund_amount": "-1"}
    )
    model = Mock()
    model.with_structured_output.return_value = runnable
    monkeypatch.setattr(reasoning, "get_model", lambda name: model)
    model_name = "openai-compatible" if compatible else "another-provider"
    with pytest.raises(ValidationError):
        await reasoning.LangChainTicketReasoner().classify(
            "退款", None, {"configurable": {"model": model_name}}
        )
    if compatible:
        args, kwargs = model.with_structured_output.call_args
        assert kwargs == {"method": "json_schema"}
        assert args[0]["properties"]["requested_refund_amount"]["anyOf"] == [
            {"type": "number"},
            {"type": "null"},
        ]
        assert set(args[0]["required"]) == set(args[0]["properties"])
        assert all("default" not in item for item in args[0]["properties"].values())
    else:
        model.with_structured_output.assert_called_once_with(TicketClassification)
