import asyncio
import sys
from decimal import Decimal

import pytest

from ticketpilot.db import apply_migrations, get_ticketpilot_pool
from ticketpilot.domain import FulfillmentStatus, OrderPaymentStatus
from ticketpilot.seed import generate_seed_orders, load_seed_manifest, seed_orders

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def test_seed_generation_is_deterministic_and_covers_boundaries() -> None:
    manifest = load_seed_manifest()
    first = generate_seed_orders(manifest)
    second = generate_seed_orders(manifest)

    assert first == second
    assert len(first) == 120
    assert len({order.id for order in first}) == 120
    assert {order.tenant_id for order in first} == {
        "tenant-demo-01",
        "tenant-demo-02",
        "tenant-demo-03",
    }
    assert sum(order.order_reference == "TP-0001" for order in first) == 3
    assert {order.payment_status for order in first} == set(OrderPaymentStatus)
    assert {order.fulfillment_status for order in first} == set(FulfillmentStatus)
    assert min(order.paid_amount for order in first) == Decimal("0.00")
    assert max(order.paid_amount for order in first) >= Decimal("9999.00")
    assert all(order.refundable_amount <= order.paid_amount for order in first)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_seed_import_is_idempotent() -> None:
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        first_result = await seed_orders(pool)
        async with pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    tenant_id, order_reference, customer_id, payment_status,
                    fulfillment_status, paid_amount, refundable_amount,
                    carrier, tracking_number, estimated_delivery_at
                FROM ticketpilot.orders
                WHERE tenant_id IN (
                    'tenant-demo-01', 'tenant-demo-02', 'tenant-demo-03'
                )
                ORDER BY tenant_id, order_reference
                """
            )
            first_rows = await cursor.fetchall()

        second_result = await seed_orders(pool)
        async with pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    tenant_id, order_reference, customer_id, payment_status,
                    fulfillment_status, paid_amount, refundable_amount,
                    carrier, tracking_number, estimated_delivery_at
                FROM ticketpilot.orders
                WHERE tenant_id IN (
                    'tenant-demo-01', 'tenant-demo-02', 'tenant-demo-03'
                )
                ORDER BY tenant_id, order_reference
                """
            )
            second_rows = await cursor.fetchall()

    assert first_result == second_result
    assert first_result.order_count == 120
    assert len(first_rows) == 120
    assert first_rows == second_rows
