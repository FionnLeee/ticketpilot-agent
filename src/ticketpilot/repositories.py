import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ticketpilot.db import BusinessConnection, BusinessPool
from ticketpilot.domain import ActorType, MessageRole, PrincipalRole, TicketStatus
from ticketpilot.errors import ResourceNotFound, StateConflict
from ticketpilot.schemas import AddTicketMessageRequest, CreateTicketRequest, RequestPrincipal


@dataclass(frozen=True)
class CreatedTicket:
    ticket: dict[str, Any]
    message: dict[str, Any]
    run_id: UUID
    should_run: bool


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

    @staticmethod
    def _create_request_hash(request: CreateTicketRequest) -> str:
        canonical = json.dumps(
            {"version": 1, **request.model_dump(mode="json")},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    async def _creation_message(
        connection: BusinessConnection, tenant_id: str, ticket_id: UUID
    ) -> dict[str, Any]:
        cursor = await connection.execute(
            """
            SELECT id, run_id, role, content, citations, created_at
            FROM ticketpilot.ticket_messages
            WHERE tenant_id = %s AND ticket_id = %s
              AND role IN ('CUSTOMER', 'STAFF') AND run_id IS NOT NULL
            ORDER BY created_at, id
            LIMIT 1
            """,
            (tenant_id, ticket_id),
        )
        message = await cursor.fetchone()
        if message is None:
            raise RuntimeError("Idempotent ticket has no creation message")
        return message

    async def create(
        self,
        principal: RequestPrincipal,
        request: CreateTicketRequest,
        idempotency_key: str,
        *,
        reserve_run: bool = False,
    ) -> CreatedTicket:
        ticket_id = uuid4()
        message_id = uuid4()
        run_id = uuid4()
        thread_id = str(uuid4())
        request_hash = self._create_request_hash(request)

        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                SELECT id, thread_id, status, processing_result, subject, category, priority,
                       risk_level, created_at, updated_at, active_run_id,
                       run_started, create_request_hash
                FROM ticketpilot.tickets
                WHERE tenant_id = %s AND create_request_actor_id = %s
                  AND create_idempotency_key = %s
                FOR UPDATE
                """,
                (principal.tenant_id, principal.actor_id, idempotency_key),
            )
            existing_ticket = await cursor.fetchone()
            if existing_ticket is not None:
                if existing_ticket["create_request_hash"] != request_hash:
                    raise StateConflict(
                        "Idempotency-Key was already used with a different ticket request"
                    )
                existing_message = await self._creation_message(
                    connection, principal.tenant_id, existing_ticket["id"]
                )
                return CreatedTicket(
                    ticket=existing_ticket,
                    message=existing_message,
                    run_id=existing_message["run_id"],
                    should_run=(
                        reserve_run
                        and existing_ticket["active_run_id"] == existing_message["run_id"]
                        and not existing_ticket["run_started"]
                    ),
                )

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
                    id, tenant_id, customer_id, order_pk, thread_id, status, subject,
                    create_request_actor_id, create_idempotency_key, create_request_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    tenant_id, create_request_actor_id, create_idempotency_key
                ) WHERE create_idempotency_key IS NOT NULL DO NOTHING
                RETURNING id, thread_id, status, processing_result, subject, category, priority,
                          risk_level, created_at, updated_at, active_run_id, run_started
                """,
                (
                    ticket_id,
                    principal.tenant_id,
                    customer_id,
                    order_pk,
                    thread_id,
                    TicketStatus.NEW.value,
                    request.subject,
                    principal.actor_id,
                    idempotency_key,
                    request_hash,
                ),
            )
            ticket = await cursor.fetchone()
            if ticket is None:
                cursor = await connection.execute(
                    """
                    SELECT id, thread_id, status, processing_result, subject, category, priority,
                           risk_level, created_at, updated_at, active_run_id,
                           run_started, create_request_hash
                    FROM ticketpilot.tickets
                    WHERE tenant_id = %s AND create_request_actor_id = %s
                      AND create_idempotency_key = %s
                    FOR UPDATE
                    """,
                    (principal.tenant_id, principal.actor_id, idempotency_key),
                )
                existing_ticket = await cursor.fetchone()
                if existing_ticket is None:
                    raise RuntimeError("Idempotent ticket could not be loaded")
                if existing_ticket["create_request_hash"] != request_hash:
                    raise StateConflict(
                        "Idempotency-Key was already used with a different ticket request"
                    )
                existing_message = await self._creation_message(
                    connection, principal.tenant_id, existing_ticket["id"]
                )
                return CreatedTicket(
                    ticket=existing_ticket,
                    message=existing_message,
                    run_id=existing_message["run_id"],
                    should_run=(
                        reserve_run
                        and existing_ticket["active_run_id"] == existing_message["run_id"]
                        and not existing_ticket["run_started"]
                    ),
                )

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

            if reserve_run:
                await connection.execute(
                    """
                    UPDATE ticketpilot.tickets SET active_run_id = %s, trigger_message_id = %s
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (run_id, message_id, principal.tenant_id, ticket_id),
                )

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
        return CreatedTicket(
            ticket=ticket,
            message=message,
            run_id=run_id,
            should_run=reserve_run,
        )

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
            MessageRole.CUSTOMER if principal.role is PrincipalRole.CUSTOMER else MessageRole.STAFF
        )
        actor_type = (
            ActorType.CUSTOMER if principal.role is PrincipalRole.CUSTOMER else ActorType.STAFF
        )

        async with self.pool.connection() as connection, connection.transaction():
            if principal.role is PrincipalRole.CUSTOMER:
                cursor = await connection.execute(
                    """
                    SELECT id, thread_id, status, active_run_id, run_started
                    FROM ticketpilot.tickets
                    WHERE tenant_id = %s AND id = %s AND customer_id = %s
                    FOR UPDATE
                    """,
                    (principal.tenant_id, ticket_id, principal.actor_id),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT id, thread_id, status, active_run_id, run_started
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
                    raise StateConflict("Idempotency-Key was already used with a different message")
                if ticket["active_run_id"] == existing["run_id"] and ticket["run_started"]:
                    raise StateConflict(
                        "This message is already executing; retry after it completes"
                    )
                return AppendedTicketMessage(
                    ticket_id=ticket_id,
                    thread_id=ticket["thread_id"],
                    message=existing,
                    run_id=existing["run_id"],
                    should_run=(
                        ticket["active_run_id"] == existing["run_id"] and not ticket["run_started"]
                    ),
                )

            if ticket["active_run_id"] is not None:
                raise StateConflict("Ticket already has an active run; retry after it completes")
            if ticket["status"] not in {
                TicketStatus.NEW.value,
                TicketStatus.WAITING_INFORMATION.value,
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
                UPDATE ticketpilot.tickets
                SET active_run_id = %s, trigger_message_id = %s, run_started = false
                WHERE tenant_id = %s AND id = %s
                """,
                (run_id, message_id, principal.tenant_id, ticket_id),
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

    async def claim_run(self, tenant_id: str, ticket_id: UUID, run_id: UUID) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                UPDATE ticketpilot.tickets SET run_started = true
                WHERE tenant_id = %s AND id = %s AND active_run_id = %s AND NOT run_started
                """,
                (tenant_id, ticket_id, run_id),
            )
            if cursor.rowcount != 1:
                raise StateConflict("Run is already executing or no longer owns the ticket")

    async def release_run(self, tenant_id: str, ticket_id: UUID, run_id: UUID) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET active_run_id = NULL, trigger_message_id = NULL, run_started = false
                WHERE tenant_id = %s AND id = %s AND active_run_id = %s AND run_started
                """,
                (tenant_id, ticket_id, run_id),
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

    async def list_tickets(
        self, principal: RequestPrincipal, limit: int
    ) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            customer_filter = "AND t.customer_id = %s" if principal.role is PrincipalRole.CUSTOMER else ""
            params: tuple[Any, ...] = (
                (principal.tenant_id, principal.actor_id, limit)
                if principal.role is PrincipalRole.CUSTOMER
                else (principal.tenant_id, limit)
            )
            cursor = await connection.execute(
                f"""
                SELECT
                    t.id, t.thread_id, t.status, t.processing_result, t.subject,
                    t.category, t.priority, t.risk_level, o.order_reference,
                    t.created_at, t.updated_at
                FROM ticketpilot.tickets AS t
                LEFT JOIN ticketpilot.orders AS o
                    ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
                WHERE t.tenant_id = %s {customer_filter}
                ORDER BY t.updated_at DESC, t.id
                LIMIT %s
                """,
                params,
            )
            return list(await cursor.fetchall())

    async def list_approvals(self, tenant_id: str, limit: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    a.id, a.ticket_id, a.run_id, t.subject, o.order_reference,
                    a.action_type, a.action_payload, a.status, a.requested_at
                FROM ticketpilot.approvals AS a
                JOIN ticketpilot.tickets AS t
                    ON t.tenant_id = a.tenant_id AND t.id = a.ticket_id
                LEFT JOIN ticketpilot.orders AS o
                    ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
                WHERE a.tenant_id = %s
                ORDER BY (a.status = 'PENDING') DESC, a.requested_at DESC, a.id
                LIMIT %s
                """,
                (tenant_id, limit),
            )
            return list(await cursor.fetchall())

    async def dashboard_summary(self, tenant_id: str) -> dict[str, Any]:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT order_count, customer_count, ticket_count, contact_rate,
                       refund_ticket_count, open_ticket_count, avg_resolution_minutes
                FROM ticketpilot.tenant_overview
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            tenant = await cursor.fetchone()
            cursor = await connection.execute(
                """
                SELECT dataset_id, loaded_at, row_counts
                FROM ticketpilot.synthetic_datasets
                ORDER BY loaded_at DESC
                """
            )
            datasets = list(await cursor.fetchall())
            cursor = await connection.execute(
                """
                SELECT day::text AS day,
                       sum(created_count)::bigint AS created_count,
                       sum(refund_count)::bigint AS refund_count,
                       sum(resolved_count)::bigint AS resolved_count,
                       sum(failed_count)::bigint AS failed_count
                FROM ticketpilot.daily_ticket_volume
                WHERE data_origin = 'SYNTHETIC_HISTORY'
                GROUP BY day
                ORDER BY day DESC
                LIMIT 30
                """
            )
            daily_volume = list(reversed(await cursor.fetchall()))
            cursor = await connection.execute(
                """
                SELECT status, sum(approval_count)::bigint AS approval_count,
                       coalesce(sum(requested_amount), 0) AS requested_amount
                FROM ticketpilot.refund_approval_funnel
                WHERE data_origin = 'SYNTHETIC_HISTORY'
                GROUP BY status
                ORDER BY status
                """
            )
            refund_funnel = list(await cursor.fetchall())
        return {
            "tenant": tenant,
            "datasets": datasets,
            "daily_volume": daily_volume,
            "refund_funnel": refund_funnel,
        }

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
