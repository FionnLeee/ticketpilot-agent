import asyncio
import sys
from decimal import Decimal

import pytest
from langgraph.prebuilt import ToolRuntime

from ticketpilot.db import apply_migrations, get_ticketpilot_pool
from ticketpilot.domain import (
    FulfillmentStatus,
    OrderPaymentStatus,
    PrincipalRole,
)
from ticketpilot.errors import Forbidden
from ticketpilot.orders import OrderReader, PostgresOrderRepository
from ticketpilot.schemas import OrderSummary, RequestPrincipal
from ticketpilot.seed import seed_orders
from ticketpilot.tools import TicketPilotContext, query_order, query_order_func

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class FakeOrderReader:
    def __init__(self, order: OrderSummary | None, delay: float = 0) -> None:
        self.order = order
        self.delay = delay
        self.call = None

    async def get_by_reference(
        self, principal: RequestPrincipal, order_reference: str
    ) -> OrderSummary | None:
        self.call = (principal, order_reference)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.order


def make_runtime(
    principal: RequestPrincipal,
    order_reader: OrderReader,
    timeout: float = 1,
) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=TicketPilotContext(
            principal=principal,
            order_reader=order_reader,
            order_timeout_seconds=timeout,
        ),
        config={},
        stream_writer=lambda _: None,
        tool_call_id=None,
        store=None,
    )


@pytest.fixture
def customer() -> RequestPrincipal:
    return RequestPrincipal(
        tenant_id="tenant-demo-01",
        actor_id="customer-demo-01",
        role=PrincipalRole.CUSTOMER,
    )


@pytest.mark.asyncio
async def test_query_order_tool_exposes_only_order_reference(customer) -> None:
    order = OrderSummary(
        order_reference="TP-0001",
        payment_status=OrderPaymentStatus.PAID,
        fulfillment_status=FulfillmentStatus.SHIPPED,
        paid_amount=Decimal("99.00"),
        refundable_amount=Decimal("99.00"),
        currency="CNY",
    )
    reader = FakeOrderReader(order)

    result = await query_order_func("TP-0001", make_runtime(customer, reader))

    assert query_order.name == "query_order"
    assert set(query_order.tool_call_schema.model_json_schema()["properties"]) == {
        "order_reference"
    }
    assert result["found"] is True
    assert result["order"]["paid_amount"] == "99.00"
    assert reader.call == (customer, "TP-0001")


@pytest.mark.asyncio
async def test_query_order_tool_classifies_not_found_and_timeout(customer) -> None:
    not_found = await query_order_func("TP-404", make_runtime(customer, FakeOrderReader(None)))
    timed_out = await query_order_func(
        "TP-SLOW",
        make_runtime(customer, FakeOrderReader(None, delay=0.05), timeout=0.001),
    )

    assert not_found["error_code"] == "ORDER_NOT_FOUND"
    assert timed_out["error_code"] == "DEPENDENCY_TIMEOUT"


@pytest.mark.docker
@pytest.mark.asyncio
async def test_postgres_order_repository_enforces_customer_and_role_scope() -> None:
    customer = RequestPrincipal(
        tenant_id="tenant-demo-01",
        actor_id="customer-demo-03",
        role=PrincipalRole.CUSTOMER,
    )
    other_customer = customer.model_copy(update={"actor_id": "customer-demo-02"})
    approver = customer.model_copy(update={"role": PrincipalRole.APPROVER})

    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        await seed_orders(pool)
        repository = PostgresOrderRepository(pool)

        order = await repository.get_by_reference(customer, "TP-0003")
        hidden_order = await repository.get_by_reference(other_customer, "TP-0003")
        with pytest.raises(Forbidden):
            await repository.get_by_reference(approver, "TP-0003")

    assert order is not None
    assert order.order_reference == "TP-0003"
    assert order.tracking_number_masked == "TR******0003"
    assert hidden_order is None
