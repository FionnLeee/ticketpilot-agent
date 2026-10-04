import asyncio
import sys
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from memory.postgres import get_postgres_connection_string
from ticketpilot.db import apply_migrations, get_ticketpilot_pool

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.mark.docker
@pytest.mark.asyncio
async def test_fresh_database_migration_initialization_is_serialized() -> None:
    database_name = f"ticketpilot_migration_test_{uuid4().hex}"
    admin = await AsyncConnection.connect(get_postgres_connection_string(), autocommit=True)
    try:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        conninfo = make_conninfo(get_postgres_connection_string(), dbname=database_name)
        locker = await AsyncConnection.connect(conninfo, autocommit=True)
        try:
            await locker.execute(
                "SELECT pg_advisory_lock(hashtext('ticketpilot-schema-migrations'))"
            )
            async with AsyncConnectionPool(
                conninfo, min_size=4, max_size=4, kwargs={"row_factory": dict_row}
            ) as pool:
                tasks = [asyncio.create_task(apply_migrations(pool)) for _ in range(4)]
                try:
                    async with asyncio.timeout(10):
                        while True:
                            cursor = await locker.execute(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname = %s AND wait_event = 'advisory'",
                                (database_name,),
                            )
                            if (await cursor.fetchone())[0] == 4:
                                break
                            await asyncio.sleep(0.02)
                    cursor = await locker.execute("SELECT to_regnamespace('ticketpilot')")
                    assert (await cursor.fetchone())[0] is None
                finally:
                    await locker.execute(
                        "SELECT pg_advisory_unlock(hashtext('ticketpilot-schema-migrations'))"
                    )
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                assert all(isinstance(result, list) for result in results), results
                assert sum(len(result) for result in results) == 8
                assert await apply_migrations(pool) == []
        finally:
            await locker.close()
    finally:
        await admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
        )
        await admin.close()


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
async def test_ticket_status_and_processing_result_must_agree() -> None:
    with pytest.raises(errors.CheckViolation):
        async with get_ticketpilot_pool() as pool, pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO ticketpilot.tickets (
                    id, tenant_id, customer_id, thread_id, status,
                    processing_result, subject
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    "tenant-result-constraint-test",
                    "customer-1",
                    str(uuid4()),
                    "RESOLVED",
                    "NEEDS_INPUT",
                    "Invalid result test",
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
    first_action_id = uuid4()

    async with get_ticketpilot_pool() as pool:
        async with pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO ticketpilot.tickets (
                    id, tenant_id, customer_id, thread_id, status,
                    processing_result, subject
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    ticket_id,
                    tenant_id,
                    "customer-1",
                    str(uuid4()),
                    "WAITING_APPROVAL",
                    "WAITING_APPROVAL",
                    "Idempotency test",
                ),
            )
            await connection.execute(
                """
                INSERT INTO ticketpilot.approvals (
                    id, tenant_id, ticket_id, run_id, action_id, action_type,
                    action_payload, status, requested_by, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    tenant_id,
                    ticket_id,
                    uuid4(),
                    first_action_id,
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
                        id, tenant_id, ticket_id, run_id, action_id, action_type,
                        action_payload, status, requested_by, idempotency_key
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        tenant_id,
                        ticket_id,
                        uuid4(),
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
