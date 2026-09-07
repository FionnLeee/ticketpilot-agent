from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ticketpilot.domain import (
    FulfillmentStatus,
    OrderPaymentStatus,
    TicketStatus,
)
from ticketpilot.privacy import mask_tracking_number
from ticketpilot.schemas import CreateTicketRequest, OrderSummary, TicketSummary


def test_create_ticket_request_strips_text_and_accepts_order_reference() -> None:
    request = CreateTicketRequest(
        subject="  查询物流  ",
        message="  订单 O-9527 什么时候送到？  ",
        order_reference="  O-9527  ",
    )

    assert request.subject == "查询物流"
    assert request.message == "订单 O-9527 什么时候送到？"
    assert request.order_reference == "O-9527"


@pytest.mark.parametrize("field", ["tenant_id", "user_id", "status"])
def test_create_ticket_request_rejects_server_controlled_fields(field: str) -> None:
    payload = {"subject": "查询物流", "message": "请查询 O-9527", field: "attacker-value"}

    with pytest.raises(ValidationError):
        CreateTicketRequest.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"subject": "", "message": "有效消息"},
        {"subject": "有效标题", "message": ""},
        {"subject": "x" * 201, "message": "有效消息"},
        {"subject": "有效标题", "message": "x" * 8001},
    ],
)
def test_create_ticket_request_rejects_invalid_lengths(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        CreateTicketRequest.model_validate(payload)


def test_order_summary_rejects_refundable_amount_above_paid_amount() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        OrderSummary(
            order_reference="O-9527",
            payment_status=OrderPaymentStatus.PAID,
            fulfillment_status=FulfillmentStatus.SHIPPED,
            paid_amount=Decimal("799.00"),
            refundable_amount=Decimal("800.00"),
            currency="CNY",
        )


def test_ticket_summary_requires_defined_status() -> None:
    now = datetime.now(UTC)
    payload = {
        "id": uuid4(),
        "thread_id": str(uuid4()),
        "status": "UNKNOWN",
        "subject": "查询物流",
        "created_at": now,
        "updated_at": now,
    }

    with pytest.raises(ValidationError):
        TicketSummary.model_validate(payload)

    payload["status"] = TicketStatus.NEW
    assert TicketSummary.model_validate(payload).status is TicketStatus.NEW


@pytest.mark.parametrize(
    ("tracking_number", "masked"),
    [
        (None, None),
        ("1234", "****"),
        ("12345", "*2345"),
        ("123456", "**3456"),
        ("SF1234567890", "SF******7890"),
    ],
)
def test_tracking_number_masking_covers_short_boundaries(
    tracking_number: str | None, masked: str | None
) -> None:
    assert mask_tracking_number(tracking_number) == masked
