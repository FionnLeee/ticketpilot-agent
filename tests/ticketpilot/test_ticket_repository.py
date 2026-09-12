import asyncio
import sys
from uuid import uuid4

import pytest

from ticketpilot.db import apply_migrations, get_ticketpilot_pool
from ticketpilot.domain import PrincipalRole, TicketStatus
from ticketpilot.errors import ResourceNotFound, StateConflict
from ticketpilot.repositories import TicketRepository
from ticketpilot.schemas import CreateTicketRequest, RequestPrincipal
from ticketpilot.services import TicketService

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.mark.docker
@pytest.mark.asyncio
async def test_ticket_create_and_get_are_atomic_and_tenant_scoped() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = RequestPrincipal(
        tenant_id=tenant_id,
        actor_id="customer-a",
        role=PrincipalRole.CUSTOMER,
    )
    other_tenant = customer.model_copy(update={"tenant_id": f"tenant-{uuid4()}"})
    other_customer = customer.model_copy(update={"actor_id": "customer-b"})

    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        service = TicketService(TicketRepository(pool))
        created = await service.create_ticket(
            customer,
            CreateTicketRequest(subject="普通咨询", message="请问退货期限？"),
            "create-atomic",
        )

        assert created.ticket.status is TicketStatus.NEW
        assert created.latest_message is not None
        detail = await service.get_ticket(customer, created.ticket.id)
        assert [message.content for message in detail.messages] == ["请问退货期限？"]
        events = await service.get_run_events(customer, created.run_id)
        assert [event.event_type for event in events.events] == ["TICKET_CREATED"]

        with pytest.raises(ResourceNotFound):
            await service.get_ticket(other_tenant, created.ticket.id)
        with pytest.raises(ResourceNotFound):
            await service.get_run_events(other_tenant, created.run_id)
        with pytest.raises(ResourceNotFound):
            await service.get_run_events(other_customer, created.run_id)

        async with pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT event_type, actor_id, details
                FROM ticketpilot.audit_events
                WHERE tenant_id = %s AND ticket_id = %s
                """,
                (tenant_id, created.ticket.id),
            )
            event = await cursor.fetchone()
            assert event == {
                "event_type": "TICKET_CREATED",
                "actor_id": "customer-a",
                "details": {"order_linked": False},
            }
            await connection.execute(
                "DELETE FROM ticketpilot.audit_events WHERE tenant_id = %s",
                (tenant_id,),
            )
            await connection.execute(
                "DELETE FROM ticketpilot.ticket_messages WHERE tenant_id = %s",
                (tenant_id,),
            )
            await connection.execute(
                "DELETE FROM ticketpilot.tickets WHERE tenant_id = %s",
                (tenant_id,),
            )


@pytest.mark.docker
@pytest.mark.asyncio
async def test_create_request_idempotency_distinguishes_retry_from_new_request() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = RequestPrincipal(
        tenant_id=tenant_id,
        actor_id="customer-a",
        role=PrincipalRole.CUSTOMER,
    )
    other_customer = customer.model_copy(update={"actor_id": "customer-b"})
    request = CreateTicketRequest(subject=" 普通咨询 ", message=" 请问退货期限？ ")

    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        service = TicketService(TicketRepository(pool))
        first, concurrent_retry = await asyncio.gather(
            service.create_ticket(customer, request, "same-create-attempt"),
            service.create_ticket(customer, request, "same-create-attempt"),
        )
        normalized_retry = await service.create_ticket(
            customer,
            CreateTicketRequest(subject="普通咨询", message="请问退货期限？"),
            "same-create-attempt",
        )

        assert first.ticket.id == concurrent_retry.ticket.id == normalized_retry.ticket.id
        assert first.run_id == concurrent_retry.run_id == normalized_retry.run_id

        with pytest.raises(StateConflict, match="different ticket request"):
            await service.create_ticket(
                customer,
                CreateTicketRequest(subject="普通咨询", message="换了正文"),
                "same-create-attempt",
            )

        new_request = await service.create_ticket(customer, request, "new-create-attempt")
        other_actor = await service.create_ticket(other_customer, request, "same-create-attempt")
        assert len({first.ticket.id, new_request.ticket.id, other_actor.ticket.id}) == 3

        async with pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM ticketpilot.tickets WHERE tenant_id = %s) AS tickets,
                    (SELECT count(*) FROM ticketpilot.ticket_messages WHERE tenant_id = %s) AS messages,
                    (SELECT count(*) FROM ticketpilot.audit_events WHERE tenant_id = %s) AS events
                """,
                (tenant_id, tenant_id, tenant_id),
            )
            assert await cursor.fetchone() == {"tickets": 3, "messages": 3, "events": 3}
            await connection.execute(
                "DELETE FROM ticketpilot.audit_events WHERE tenant_id = %s", (tenant_id,)
            )
            await connection.execute(
                "DELETE FROM ticketpilot.ticket_messages WHERE tenant_id = %s", (tenant_id,)
            )
            await connection.execute(
                "DELETE FROM ticketpilot.tickets WHERE tenant_id = %s", (tenant_id,)
            )


@pytest.mark.docker
@pytest.mark.asyncio
async def test_explicit_order_reference_requires_customer_ownership() -> None:
    tenant_id = f"tenant-{uuid4()}"
    order_id = uuid4()
    order_reference = f"O-{uuid4()}"
    owner = RequestPrincipal(
        tenant_id=tenant_id,
        actor_id="customer-owner",
        role=PrincipalRole.CUSTOMER,
    )
    other_customer = owner.model_copy(update={"actor_id": "customer-other"})

    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
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
                    order_id,
                    tenant_id,
                    order_reference,
                    owner.actor_id,
                    "PAID",
                    "SHIPPED",
                    "799.00",
                    "799.00",
                    "CNY",
                ),
            )

        service = TicketService(TicketRepository(pool))
        request = CreateTicketRequest(
            subject="查询物流",
            message="什么时候送到？",
            order_reference=order_reference,
        )
        with pytest.raises(ResourceNotFound):
            await service.create_ticket(other_customer, request, "create-other-customer")

        created = await service.create_ticket(owner, request, "create-owner")
        detail = await service.get_ticket(owner, created.ticket.id)
        assert detail.order is not None
        assert detail.order.order_reference == order_reference
        assert detail.order.tracking_number_masked is None

        async with pool.connection() as connection:
            await connection.execute(
                "DELETE FROM ticketpilot.audit_events WHERE tenant_id = %s",
                (tenant_id,),
            )
            await connection.execute(
                "DELETE FROM ticketpilot.ticket_messages WHERE tenant_id = %s",
                (tenant_id,),
            )
            await connection.execute(
                "DELETE FROM ticketpilot.tickets WHERE tenant_id = %s",
                (tenant_id,),
            )
            await connection.execute(
                "DELETE FROM ticketpilot.orders WHERE tenant_id = %s",
                (tenant_id,),
            )
