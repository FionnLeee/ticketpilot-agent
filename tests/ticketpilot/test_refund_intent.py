from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from ticketpilot import graph
from ticketpilot.domain import TicketCategory
from ticketpilot.reasoning import DeterministicDemoReasoner
from ticketpilot.schemas import TicketClassification


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,category",
    [
        ("我不需要退款，只想知道物流", TicketCategory.ORDER_STATUS),
        ("TP-0001 的退款规则是什么？我只是咨询", TicketCategory.POLICY),
        ("退款只是举例", TicketCategory.OTHER),
    ],
)
async def test_keywords_do_not_override_model_intent(message, category):
    classification = TicketClassification(category=category, order_reference="TP-0001")
    runtime = SimpleNamespace(
        context=SimpleNamespace(
            reasoner=SimpleNamespace(classify=AsyncMock(return_value=classification)),
            workflow_repository=SimpleNamespace(save_triage=AsyncMock()),
            principal=SimpleNamespace(tenant_id="test"),
        )
    )
    state = {
        "ticket_id": str(uuid4()),
        "run_id": str(uuid4()),
        "customer_message": message,
        "order_reference": "TP-0001",
        "order_result": {"found": True, "order": {"refundable_amount": "399"}},
    }
    with patch.object(graph, "get_config", return_value={}):
        state.update(await graph.classify_request(state, runtime))
    state.update(graph.plan_work(state))
    assert state["classification"]["category"] == category.value
    assert state["risk_level"] == "READ_ONLY"
    assert graph.route_work(state) == "answer"
    assert not state["proposed_refund_amount"]


@pytest.mark.parametrize(
    "amount,full,expected",
    [
        (None, False, ""),
        (Decimal("100"), False, "100"),
        (None, True, "399"),
        (Decimal("100"), True, ""),
    ],
)
def test_refund_amount_requires_explicit_amount_or_full_intent(amount, full, expected):
    state = {
        "classification": TicketClassification(
            category=TicketCategory.REFUND,
            requested_refund_amount=amount,
            full_refund_requested=full,
        ).model_dump(mode="json"),
        "order_reference": "TP-0001",
        "order_result": {"found": True, "order": {"refundable_amount": "399"}},
    }
    update = graph.plan_work(state)
    assert update["proposed_refund_amount"] == expected
    if not expected:
        assert update["clarification"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,category,amount,full",
    [
        ("我不需要退款，只想知道物流", "ORDER_STATUS", None, False),
        ("TP-0001 的退款规则是什么？我只是咨询", "POLICY", None, False),
        ("我想退一部分", "REFUND", None, False),
        ("退款，已经等了3天", "REFUND", None, False),
        ("申请全额退款", "REFUND", None, True),
        ("申请退款100元", "REFUND", Decimal("100"), False),
        ("算了，不退了", "OTHER", None, False),
    ],
)
async def test_demo_reasoner_semantics(message, category, amount, full):
    result = await DeterministicDemoReasoner().classify(message, "TP-0001", {})
    assert result.category.value == category
    assert result.requested_refund_amount == amount
    assert result.full_refund_requested is full


@pytest.mark.asyncio
async def test_explicit_pending_slot_does_not_need_another_model_call():
    classify = AsyncMock(side_effect=AssertionError("No model call needed for an exact slot"))
    runtime = SimpleNamespace(
        context=SimpleNamespace(
            reasoner=SimpleNamespace(classify=classify),
            workflow_repository=SimpleNamespace(save_triage=AsyncMock()),
            principal=SimpleNamespace(tenant_id="test"),
        )
    )
    state = {
        "ticket_id": str(uuid4()),
        "run_id": str(uuid4()),
        "customer_message": "TP-0001",
        "order_reference": None,
        "pending_request": TicketClassification(
            category=TicketCategory.REFUND, requested_refund_amount=Decimal("100")
        ).model_dump(mode="json"),
    }
    with patch.object(graph, "get_config", return_value={}):
        update = await graph.classify_request(state, runtime)
    classify.assert_not_awaited()
    assert update["classification"]["requested_refund_amount"] == "100"
    assert update["order_reference"] == "TP-0001"
