from decimal import Decimal

import pytest

from ticketpilot.domain import TicketCategory
from ticketpilot.reasoning import DeterministicDemoReasoner
from ticketpilot.schemas import Citation


@pytest.mark.asyncio
async def test_deterministic_demo_reasoner_extracts_refund_without_using_order_digits() -> None:
    reasoner = DeterministicDemoReasoner()

    result = await reasoner.classify("订单 TP-0002 退款 100.50 元", None, {})

    assert result.category is TicketCategory.REFUND
    assert result.order_reference == "TP-0002"
    assert result.requested_refund_amount == Decimal("100.50")


@pytest.mark.asyncio
async def test_deterministic_demo_answer_uses_only_supplied_order_and_citation() -> None:
    reasoner = DeterministicDemoReasoner()
    classification = await reasoner.classify("查询 TP-0003 物流", None, {})

    answer = await reasoner.answer(
        "查询 TP-0003 物流",
        classification,
        {
            "found": True,
            "order": {
                "order_reference": "TP-0003",
                "payment_status": "PAID",
                "fulfillment_status": "SHIPPED",
                "carrier": "SF Express",
                "estimated_delivery_at": None,
            },
        },
        [Citation(source_id="policy-v1", title="物流规则", chunk_id="order-tracking")],
        {},
    )

    assert "TP-0003" in answer
    assert "SHIPPED" in answer
    assert "order-tracking" in answer
