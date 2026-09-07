from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ticketpilot.db import BusinessPool
from ticketpilot.domain import ActorType, MessageRole, PrincipalRole, TicketStatus
from ticketpilot.errors import ResourceNotFound, StateConflict
from ticketpilot.schemas import AddTicketMessageRequest, CreateTicketRequest, RequestPrincipal


@dataclass(frozen=True)
class CreatedTicket:
    ticket: dict[str, Any]
    message: dict[str, Any]
    run_id: UUID


@dataclass(frozen=True)
class AppendedTicketMessage:
    ticket_id: UUID
    thread_id: str
    message: dict[str, Any]
    run_id: UUID
    should_run: bool


class TicketRepository:
    def __init__(self, pool: BusinessPool) -> None:
        self.pool = pool

    async def create(
        self, principal: RequestPrincipal, request: CreateTicketRequest
    ) -> CreatedTicket:
        ticket_id = uuid4()
        message_id = uuid4()
        run_id = uuid4()
        thread_id = str(uuid4())

        async with self.pool.connection() as connection, connection.transaction():
            order = None
            if request.order_reference is not None:
                if principal.role is PrincipalRole.CUSTOMER:
                    cursor = await connection.execute(
                        """
                        SELECT id, customer_id
                        FROM ticketpilot.orders
                        WHERE tenant_id = %s AND order_reference = %s AND customer_id = %s
                        """,
                        (principal.tenant_id, request.order_reference, principal.actor_id),
                    )
                else:
                    cursor = await connection.execute(
                        """
                        SELECT id, customer_id
                        FROM ticketpilot.orders
                        WHERE tenant_id = %s AND order_reference = %s
                        """,
                        (principal.tenant_id, request.order_reference),
                    )
                order = await cursor.fetchone()
                if order is None:
                    raise ResourceNotFound("Order")

            customer_id = order["customer_id"] if order else principal.actor_id
            order_pk = order["id"] if order else None
            cursor = await connection.execute(
                """
                INSERT INTO ticketpilot.tickets (
                    id, tenant_id, customer_id, order_pk, thread_id, status, subject
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, thread_id, status, subject, category, priority,
                          risk_level, created_at, updated_at
                """,
                (
                    ticket_id,
                    principal.tenant_id,
                    customer_id,
                    order_pk,
                    thread_id,
                    TicketStatus.NEW.value,
                    request.subject,
                ),
            )
            ticket = await cursor.fetchone()

            cursor = await connection.execute(
                """
                INSERT INTO ticketpilot.ticket_messages (
                    id, tenant_id, ticket_id, run_id, role, content, citations
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, run_id, role, content, citations, created_at
                """,
                (
                    message_id,
                    principal.tenant_id,
                    ticket_id,
                    run_id,
                    MessageRole.CUSTOMER.value,
                    request.message,
                    Jsonb([]),
                ),
            )
            message = await cursor.fetchone()

            actor_type = (
                ActorType.CUSTOMER if principal.role is PrincipalRole.CUSTOMER else ActorType.STAFF
            )
            await connection.execute(
                """
                INSERT INTO ticketpilot.audit_events (
                    id, tenant_id, ticket_id, run_id, actor_type, actor_id,
                    event_type, outcome, details
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    principal.tenant_id,
                    ticket_id,
                    run_id,
                    actor_type.value,
                    principal.actor_id,
                    "TICKET_CREATED",
                    "SUCCEEDED",
                    Jsonb({"order_linked": order_pk is not None}),
                ),
            )

        if ticket is None or message is None:
            raise RuntimeError("Ticket creation did not return persisted rows")
        return CreatedTicket(ticket=ticket, message=message, run_id=run_id)

    async def append_message(
        self,
        principal: RequestPrincipal,
        ticket_id: UUID,
        request: AddTicketMessageRequest,
        idempotency_key: str,
    ) -> AppendedTicketMessage:
        run_id = uuid4()
        message_id = uuid4()
        message_role = (
            MessageRole.CUSTOMER
            if principal.role is PrincipalRole.CUSTOMER
            else MessageRole.STAFF
        )
        actor_type = (
            ActorType.CUSTOMER
            if principal.role is PrincipalRole.CUSTOMER
            else ActorType.STAFF
        )

        async with self.pool.connection() as connection, connection.transaction():
            if principal.role is PrincipalRole.CUSTOMER:
                cursor = await connection.execute(
                    """
                    SELECT id, thread_id, status
                    FROM ticketpilot.tickets
                    WHERE tenant_id = %s AND id = %s AND customer_id = %s
                    FOR UPDATE
                    """,
                    (principal.tenant_id, ticket_id, principal.actor_id),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT id, thread_id, status
                    FROM ticketpilot.tickets
                    WHERE tenant_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (principal.tenant_id, ticket_id),
                )
            ticket = await cursor.fetchone()
            if ticket is None:
                raise ResourceNotFound("Ticket")

            cursor = await connection.execute(
                """
                SELECT id, run_id, role, content, citations, created_at
                FROM ticketpilot.ticket_messages
                WHERE tenant_id = %s AND ticket_id = %s AND idempotency_key = %s
                """,
                (principal.tenant_id, ticket_id, idempotency_key),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if existing["content"] != request.message or existing["role"] != message_role.value:
                    raise StateConflict(
                        "Idempotency-Key was already used with a different message"
                    )
                cursor = await connection.execute(
                    """
                    SELECT 1
                    FROM ticketpilot.audit_events
                    WHERE tenant_id = %s AND ticket_id = %s AND run_id = %s
                      AND event_type IN (
                          'TICKET_RESOLVED', 'REFUND_APPROVAL_REQUESTED', 'RUN_FAILED'
                      )
                    LIMIT 1
                    """,
                    (principal.tenant_id, ticket_id, existing["run_id"]),
                )
                terminal_event = await cursor.fetchone()
                return AppendedTicketMessage(
                    ticket_id=ticket_id,
                    thread_id=ticket["thread_id"],
                    message=existing,
                    run_id=existing["run_id"],
                    should_run=(
                        terminal_event is None
                        and ticket["status"]
                        in {
                            TicketStatus.NEW.value,
                            TicketStatus.RESOLVED.value,
                            TicketStatus.FAILED.value,
                        }
                    ),
                )

            if ticket["status"] not in {
                TicketStatus.NEW.value,
                TicketStatus.RESOLVED.value,
                TicketStatus.FAILED.value,
            }:
                raise StateConflict(f"Ticket cannot accept a message from {ticket['status']}")

            cursor = await connection.execute(
                """
                INSERT INTO ticketpilot.ticket_messages (
                    id, tenant_id, ticket_id, run_id, role, content,
                    citations, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, run_id, role, content, citations, created_at
                """,
                (
                    message_id,
                    principal.tenant_id,
                    ticket_id,
                    run_id,
                    message_role.value,
                    request.message,
                    Jsonb([]),
                    idempotency_key,
                ),
            )
            message = await cursor.fetchone()
            await connection.execute(
                """
                INSERT INTO ticketpilot.audit_events (
                    id, tenant_id, ticket_id, run_id, actor_type, actor_id,
                    event_type, outcome, details
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    principal.tenant_id,
                    ticket_id,
                    run_id,
                    actor_type.value,
                    principal.actor_id,
                    "MESSAGE_ADDED",
                    "SUCCEEDED",
                    Jsonb({"message_role": message_role.value}),
                ),
            )

        if message is None:
            raise RuntimeError("Message append did not return a persisted row")
        return AppendedTicketMessage(
            ticket_id=ticket_id,
            thread_id=ticket["thread_id"],
            message=message,
            run_id=run_id,
            should_run=True,
        )

    async def get_ticket(
        self, principal: RequestPrincipal, ticket_id: UUID
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            if principal.role is PrincipalRole.CUSTOMER:
                cursor = await connection.execute(
                    """
                    SELECT
                        t.*,
                        o.order_reference,
                        o.payment_status,
                        o.fulfillment_status,
                        o.paid_amount,
                        o.refundable_amount,
                        o.currency,
                        o.carrier,
                        o.tracking_number,
                        o.estimated_delivery_at
                    FROM ticketpilot.tickets AS t
                    LEFT JOIN ticketpilot.orders AS o
                        ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
                    WHERE t.tenant_id = %s AND t.id = %s AND t.customer_id = %s
                    """,
                    (principal.tenant_id, ticket_id, principal.actor_id),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT
                        t.*,
                        o.order_reference,
                        o.payment_status,
                        o.fulfillment_status,
                        o.paid_amount,
                        o.refundable_amount,
                        o.currency,
                        o.carrier,
                        o.tracking_number,
                        o.estimated_delivery_at
                    FROM ticketpilot.tickets AS t
                    LEFT JOIN ticketpilot.orders AS o
                        ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
                    WHERE t.tenant_id = %s AND t.id = %s
                    """,
                    (principal.tenant_id, ticket_id),
                )
            return await cursor.fetchone()

    async def list_messages(self, tenant_id: str, ticket_id: UUID) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT id, run_id, role, content, citations, created_at
                FROM ticketpilot.ticket_messages
                WHERE tenant_id = %s AND ticket_id = %s
                ORDER BY created_at, id
                """,
                (tenant_id, ticket_id),
            )
            return list(await cursor.fetchall())

    async def get_pending_approval(self, tenant_id: str, ticket_id: UUID) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    id, action_type, status, action_payload, requested_at,
                    decided_by, decision_reason, decided_at, executed_at
                FROM ticketpilot.approvals
                WHERE tenant_id = %s AND ticket_id = %s AND status = 'PENDING'
                ORDER BY requested_at DESC, id
                LIMIT 1
                """,
                (tenant_id, ticket_id),
            )
            return await cursor.fetchone()

    async def list_run_events(
        self, principal: RequestPrincipal, run_id: UUID
    ) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            if principal.role is PrincipalRole.CUSTOMER:
                cursor = await connection.execute(
                    """
                    SELECT
                        e.id, e.ticket_id, e.run_id, e.approval_id,
                        e.actor_type, e.actor_id, e.event_type, e.node_name,
                        e.tool_name, e.outcome, e.details, e.occurred_at
                    FROM ticketpilot.audit_events AS e
                    JOIN ticketpilot.tickets AS t
                      ON t.tenant_id = e.tenant_id AND t.id = e.ticket_id
                    WHERE e.tenant_id = %s AND e.run_id = %s
                      AND t.customer_id = %s
                    ORDER BY e.occurred_at, e.id
                    """,
                    (principal.tenant_id, run_id, principal.actor_id),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT
                        id, ticket_id, run_id, approval_id,
                        actor_type, actor_id, event_type, node_name,
                        tool_name, outcome, details, occurred_at
                    FROM ticketpilot.audit_events
                    WHERE tenant_id = %s AND run_id = %s
                    ORDER BY occurred_at, id
                    """,
                    (principal.tenant_id, run_id),
                )
            return list(await cursor.fetchall())
