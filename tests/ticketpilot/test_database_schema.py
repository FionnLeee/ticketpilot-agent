import asyncio
import sys
from uuid import uuid4

import pytest
from psycopg import errors

from ticketpilot.db import apply_migrations, get_ticketpilot_pool

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.mark.docker
@pytest.mark.asyncio
async def test_ticketpilot_migration_is_idempotent_and_creates_business_tables() -> None:
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        assert await apply_migrations(pool) == []

        async with pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'ticketpilot'
                """
            )
            rows = await cursor.fetchall()

    actual = {row["table_name"] for row in rows}
    assert {
        "orders",
        "tickets",
        "ticket_messages",
        "approvals",
        "audit_events",
        "schema_migrations",
    } <= actual


@pytest.mark.docker
@pytest.mark.asyncio
async def test_orders_reject_refundable_amount_above_paid_amount() -> None:
    with pytest.raises(errors.CheckViolation):
        async with get_ticketpilot_pool() as pool, pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO ticketpilot.orders (
                    id, tenant_id, order_reference, customer_id,
                    payment_status, fulfillment_status,
                    paid_amount, refundable_amount, currency
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    "tenant-constraint-test",
                    f"O-{uuid4()}",
                    "customer-1",
                    "PAID",
                    "SHIPPED",
                    "100.00",
                    "101.00",
                    "CNY",
                ),
            )


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tickets_reject_unknown_status() -> None:
    with pytest.raises(errors.CheckViolation):
        async with get_ticketpilot_pool() as pool, pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO ticketpilot.tickets (
                    id, tenant_id, customer_id, thread_id, status, subject
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    "tenant-constraint-test",
                    "customer-1",
                    str(uuid4()),
                    "UNKNOWN",
                    "Invalid status test",
                ),
            )


@pytest.mark.docker
@pytest.mark.asyncio
async def test_ticket_order_foreign_key_cannot_cross_tenants() -> None:
    order_pk = uuid4()
    ticket_id = uuid4()
    order_reference = f"O-{uuid4()}"

    async with get_ticketpilot_pool() as pool:
        async with pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO ticketpilot.orders (
                    id, tenant_id, order_reference, customer_id,
                    payment_status, fulfillment_status,
                    paid_amount, refundable_amount, currency
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    order_pk,
                    "tenant-a",
                    order_reference,
                    "customer-1",
                    "PAID",
                    "SHIPPED",
                    "100.00",
                    "100.00",
                    "CNY",
                ),
            )

        with pytest.raises(errors.ForeignKeyViolation):
            async with pool.connection() as connection:
                await connection.execute(
                    """
                    INSERT INTO ticketpilot.tickets (
                        id, tenant_id, customer_id, order_pk, thread_id, status, subject
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ticket_id,
                        "tenant-b",
                        "customer-1",
                        order_pk,
                        str(uuid4()),
                        "NEW",
                        "Cross-tenant reference test",
                    ),
                )

        async with pool.connection() as connection:
            await connection.execute("DELETE FROM ticketpilot.orders WHERE id = %s", (order_pk,))


@pytest.mark.docker
@pytest.mark.asyncio
async def test_approvals_enforce_tenant_idempotency_key() -> None:
    ticket_id = uuid4()
    tenant_id = "tenant-idempotency-test"
    idempotency_key = f"refund-{uuid4()}"

    async with get_ticketpilot_pool() as pool:
        async with pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO ticketpilot.tickets (
                    id, tenant_id, customer_id, thread_id, status, subject
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    ticket_id,
                    tenant_id,
                    "customer-1",
                    str(uuid4()),
                    "WAITING_APPROVAL",
                    "Idempotency test",
                ),
            )
            await connection.execute(
                """
                INSERT INTO ticketpilot.approvals (
                    id, tenant_id, ticket_id, run_id, action_type,
                    action_payload, status, requested_by, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    tenant_id,
                    ticket_id,
                    uuid4(),
                    "REFUND",
                    '{"amount": "100.00", "currency": "CNY"}',
                    "PENDING",
                    "AGENT",
                    idempotency_key,
                ),
            )

        with pytest.raises(errors.UniqueViolation):
            async with pool.connection() as connection:
                await connection.execute(
                    """
                    INSERT INTO ticketpilot.approvals (
                        id, tenant_id, ticket_id, run_id, action_type,
                        action_payload, status, requested_by, idempotency_key
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        tenant_id,
                        ticket_id,
                        uuid4(),
                        "REFUND",
                        '{"amount": "100.00", "currency": "CNY"}',
                        "PENDING",
                        "AGENT",
                        idempotency_key,
                    ),
                )

        async with pool.connection() as connection:
            await connection.execute(
                "DELETE FROM ticketpilot.approvals WHERE tenant_id = %s AND ticket_id = %s",
                (tenant_id, ticket_id),
            )
            await connection.execute(
                "DELETE FROM ticketpilot.tickets WHERE tenant_id = %s AND id = %s",
                (tenant_id, ticket_id),
            )
