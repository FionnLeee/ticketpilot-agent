from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ticketpilot.db import BusinessConnection, BusinessPool
from ticketpilot.domain import (
    ActorType,
    ApprovalDecision,
    ApprovalStatus,
    AuditOutcome,
    MessageRole,
    OrderPaymentStatus,
    PrincipalRole,
    RiskLevel,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from ticketpilot.errors import Forbidden, ResourceNotFound, StateConflict
from ticketpilot.schemas import RequestPrincipal


@dataclass(frozen=True)
class WorkflowTicket:
    id: UUID
    thread_id: str
    customer_id: str
    customer_message: str
    order_reference: str | None


@dataclass(frozen=True)
class PendingApproval:
    id: UUID
    action_payload: dict[str, Any]


@dataclass(frozen=True)
class ApprovalDecisionResult:
    approval_id: UUID
    ticket_id: UUID
    thread_id: str
    status: ApprovalStatus
    should_resume: bool


@dataclass(frozen=True)
class RefundExecutionResult:
    executed: bool
    message: str


class TicketWorkflowRepository:
    def __init__(self, pool: BusinessPool) -> None:
        self.pool = pool

    async def begin_run(
        self, principal: RequestPrincipal, ticket_id: UUID, run_id: UUID
    ) -> WorkflowTicket:
        if principal.role not in {
            PrincipalRole.CUSTOMER,
            PrincipalRole.STAFF,
            PrincipalRole.ADMIN,
        }:
            raise Forbidden
        async with self.pool.connection() as connection, connection.transaction():
            if principal.role is PrincipalRole.CUSTOMER:
                cursor = await connection.execute(
                    """
                    SELECT t.id, t.thread_id, t.customer_id, t.status, o.order_reference,
                           (
                               SELECT m.content
                               FROM ticketpilot.ticket_messages AS m
                               WHERE m.tenant_id = t.tenant_id
                                 AND m.ticket_id = t.id
                                 AND m.role IN ('CUSTOMER', 'STAFF')
                               ORDER BY m.created_at DESC, m.id DESC
                               LIMIT 1
                           ) AS customer_message
                    FROM ticketpilot.tickets AS t
                    LEFT JOIN ticketpilot.orders AS o
                      ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
                    WHERE t.tenant_id = %s AND t.id = %s AND t.customer_id = %s
                    FOR UPDATE OF t
                    """,
                    (principal.tenant_id, ticket_id, principal.actor_id),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT t.id, t.thread_id, t.customer_id, t.status, o.order_reference,
                           (
                               SELECT m.content
                               FROM ticketpilot.ticket_messages AS m
                               WHERE m.tenant_id = t.tenant_id
                                 AND m.ticket_id = t.id
                                 AND m.role IN ('CUSTOMER', 'STAFF')
                               ORDER BY m.created_at DESC, m.id DESC
                               LIMIT 1
                           ) AS customer_message
                    FROM ticketpilot.tickets AS t
                    LEFT JOIN ticketpilot.orders AS o
                      ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
                    WHERE t.tenant_id = %s AND t.id = %s
                    FOR UPDATE OF t
                    """,
                    (principal.tenant_id, ticket_id),
                )
            row = await cursor.fetchone()
            if row is None:
                raise ResourceNotFound("Ticket")
            if row["status"] not in {
                TicketStatus.NEW.value,
                TicketStatus.RESOLVED.value,
                TicketStatus.FAILED.value,
            }:
                raise StateConflict(f"Ticket cannot start a run from {row['status']}")
            if not row["customer_message"]:
                raise StateConflict("Ticket has no customer message to process")

            await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET status = %s, version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (TicketStatus.PROCESSING.value, principal.tenant_id, ticket_id),
            )
            await self._append_audit(
                connection,
                principal.tenant_id,
                ticket_id,
                run_id,
                "RUN_STARTED",
                AuditOutcome.STARTED,
                actor_type=(
                    ActorType.CUSTOMER
                    if principal.role is PrincipalRole.CUSTOMER
                    else ActorType.STAFF
                ),
                actor_id=principal.actor_id,
                node_name="load_ticket_context",
            )
        return WorkflowTicket(
            id=row["id"],
            thread_id=row["thread_id"],
            customer_id=row["customer_id"],
            customer_message=row["customer_message"],
            order_reference=row["order_reference"],
        )

    async def save_triage(
        self,
        tenant_id: str,
        ticket_id: UUID,
        run_id: UUID,
        category: TicketCategory,
        priority: TicketPriority,
        risk_level: RiskLevel,
    ) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET category = %s, priority = %s, risk_level = %s,
                    triaged_at = now(), version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s AND status = %s
                """,
                (
                    category.value,
                    priority.value,
                    risk_level.value,
                    tenant_id,
                    ticket_id,
                    TicketStatus.PROCESSING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflict("Ticket is not processing during triage")
            await self._append_audit(
                connection,
                tenant_id,
                ticket_id,
                run_id,
                "TICKET_TRIAGED",
                AuditOutcome.SUCCEEDED,
                actor_type=ActorType.AGENT,
                node_name="classify_request",
                details={
                    "category": category.value,
                    "priority": priority.value,
                    "risk_level": risk_level.value,
                },
            )

    async def link_order(
        self,
        principal: RequestPrincipal,
        ticket_id: UUID,
        run_id: UUID,
        order_reference: str,
    ) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            if principal.role is PrincipalRole.CUSTOMER:
                cursor = await connection.execute(
                    """
                    SELECT t.order_pk, o.id AS order_id, o.customer_id
                    FROM ticketpilot.tickets AS t
                    JOIN ticketpilot.orders AS o
                      ON o.tenant_id = t.tenant_id AND o.order_reference = %s
                     AND o.customer_id = %s
                    WHERE t.tenant_id = %s AND t.id = %s AND t.customer_id = %s
                      AND t.status = %s
                    FOR UPDATE OF t, o
                    """,
                    (
                        order_reference,
                        principal.actor_id,
                        principal.tenant_id,
                        ticket_id,
                        principal.actor_id,
                        TicketStatus.PROCESSING.value,
                    ),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT t.order_pk, o.id AS order_id, o.customer_id
                    FROM ticketpilot.tickets AS t
                    JOIN ticketpilot.orders AS o
                      ON o.tenant_id = t.tenant_id AND o.order_reference = %s
                    WHERE t.tenant_id = %s AND t.id = %s AND t.status = %s
                    FOR UPDATE OF t, o
                    """,
                    (
                        order_reference,
                        principal.tenant_id,
                        ticket_id,
                        TicketStatus.PROCESSING.value,
                    ),
                )
            row = await cursor.fetchone()
            if row is None:
                raise ResourceNotFound("Authorized ticket order")
            if row["order_pk"] is not None:
                if row["order_pk"] != row["order_id"]:
                    raise StateConflict("Ticket is already linked to a different order")
                return

            await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET order_pk = %s, customer_id = %s,
                    version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (
                    row["order_id"],
                    row["customer_id"],
                    principal.tenant_id,
                    ticket_id,
                ),
            )
            await self._append_audit(
                connection,
                principal.tenant_id,
                ticket_id,
                run_id,
                "ORDER_LINKED",
                AuditOutcome.SUCCEEDED,
                actor_type=ActorType.SYSTEM,
                node_name="capture_order_result",
                details={"order_reference": order_reference},
            )

    async def resolve_ticket(
        self,
        tenant_id: str,
        ticket_id: UUID,
        run_id: UUID,
        message: str,
        citations: list[dict[str, Any]],
    ) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            pending_cursor = await connection.execute(
                """
                SELECT 1 FROM ticketpilot.approvals
                WHERE tenant_id = %s AND ticket_id = %s AND status = 'PENDING'
                """,
                (tenant_id, ticket_id),
            )
            if await pending_cursor.fetchone():
                raise StateConflict("A ticket with a pending approval cannot be resolved")
            cursor = await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET status = %s, resolution_summary = %s, resolved_at = now(),
                    version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s AND status = %s
                """,
                (
                    TicketStatus.RESOLVED.value,
                    message,
                    tenant_id,
                    ticket_id,
                    TicketStatus.PROCESSING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflict("Ticket is not processing during resolution")
            await self._append_agent_message(
                connection, tenant_id, ticket_id, run_id, message, citations
            )
            await self._append_audit(
                connection,
                tenant_id,
                ticket_id,
                run_id,
                "TICKET_RESOLVED",
                AuditOutcome.SUCCEEDED,
                actor_type=ActorType.AGENT,
                node_name="finalize_ticket",
                details={"citation_count": len(citations)},
            )

    async def create_pending_refund(
        self,
        principal: RequestPrincipal,
        ticket_id: UUID,
        run_id: UUID,
        order_reference: str,
        amount: Decimal,
    ) -> PendingApproval:
        if amount <= 0:
            raise StateConflict("Refund amount must be positive")
        async with self.pool.connection() as connection, connection.transaction():
            if principal.role is PrincipalRole.CUSTOMER:
                cursor = await connection.execute(
                    """
                    SELECT t.status, t.order_pk, o.order_reference, o.currency,
                           o.payment_status, o.refundable_amount
                    FROM ticketpilot.tickets AS t
                    JOIN ticketpilot.orders AS o
                      ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
                    WHERE t.tenant_id = %s AND t.id = %s AND t.customer_id = %s
                    FOR UPDATE OF t, o
                    """,
                    (principal.tenant_id, ticket_id, principal.actor_id),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT t.status, t.order_pk, o.order_reference, o.currency,
                           o.payment_status, o.refundable_amount
                    FROM ticketpilot.tickets AS t
                    JOIN ticketpilot.orders AS o
                      ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
                    WHERE t.tenant_id = %s AND t.id = %s
                    FOR UPDATE OF t, o
                    """,
                    (principal.tenant_id, ticket_id),
                )
            row = await cursor.fetchone()
            if row is None:
                raise ResourceNotFound("Ticket order")
            if row["status"] != TicketStatus.PROCESSING.value:
                raise StateConflict("Ticket is not processing during approval creation")
            if row["order_reference"] != order_reference:
                raise StateConflict("Refund proposal does not match the ticket order")
            if (
                row["payment_status"]
                not in {
                    OrderPaymentStatus.PAID.value,
                    OrderPaymentStatus.PARTIALLY_REFUNDED.value,
                }
                or amount > row["refundable_amount"]
            ):
                raise StateConflict("Order is not eligible for the proposed refund")

            idempotency_key = (
                f"refund:{ticket_id}:{row['order_pk']}:{amount.normalize()}:{row['currency']}"
            )
            approval_id = uuid4()
            action_payload = {
                "order_id": str(row["order_pk"]),
                "order_reference": order_reference,
                "amount": str(amount),
                "currency": row["currency"],
            }
            cursor = await connection.execute(
                """
                INSERT INTO ticketpilot.approvals (
                    id, tenant_id, ticket_id, run_id, action_type, action_payload,
                    status, requested_by, idempotency_key
                ) VALUES (%s, %s, %s, %s, 'REFUND', %s, %s, %s, %s)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING id, action_payload
                """,
                (
                    approval_id,
                    principal.tenant_id,
                    ticket_id,
                    run_id,
                    Jsonb(action_payload),
                    ApprovalStatus.PENDING.value,
                    principal.actor_id,
                    idempotency_key,
                ),
            )
            approval = await cursor.fetchone()
            if approval is None:
                cursor = await connection.execute(
                    """
                    SELECT id, action_payload
                    FROM ticketpilot.approvals
                    WHERE tenant_id = %s AND idempotency_key = %s
                    """,
                    (principal.tenant_id, idempotency_key),
                )
                approval = await cursor.fetchone()
                if approval is None:
                    raise RuntimeError("Idempotent approval could not be loaded")

            await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET status = %s, version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (TicketStatus.WAITING_APPROVAL.value, principal.tenant_id, ticket_id),
            )
            await self._append_audit(
                connection,
                principal.tenant_id,
                ticket_id,
                run_id,
                "REFUND_APPROVAL_REQUESTED",
                AuditOutcome.BLOCKED,
                actor_type=ActorType.AGENT,
                approval_id=approval["id"],
                node_name="create_pending_approval",
                details={"amount": str(amount), "currency": row["currency"]},
            )
        return PendingApproval(id=approval["id"], action_payload=approval["action_payload"])

    async def decide_approval(
        self,
        principal: RequestPrincipal,
        approval_id: UUID,
        decision: ApprovalDecision,
        reason: str,
        run_id: UUID,
    ) -> ApprovalDecisionResult:
        if principal.role not in {PrincipalRole.APPROVER, PrincipalRole.ADMIN}:
            raise Forbidden("Only an approver or admin can decide an approval")
        target = (
            ApprovalStatus.APPROVED
            if decision is ApprovalDecision.APPROVE
            else ApprovalStatus.REJECTED
        )
        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                SELECT a.status, a.ticket_id, t.thread_id, t.status AS ticket_status
                FROM ticketpilot.approvals AS a
                JOIN ticketpilot.tickets AS t
                  ON t.tenant_id = a.tenant_id AND t.id = a.ticket_id
                WHERE a.tenant_id = %s AND a.id = %s
                FOR UPDATE OF a, t
                """,
                (principal.tenant_id, approval_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ResourceNotFound("Approval")
            current = ApprovalStatus(row["status"])
            same_final_decision = current is target or (
                decision is ApprovalDecision.APPROVE
                and current in {ApprovalStatus.EXECUTED, ApprovalStatus.CANCELLED}
            )
            if same_final_decision:
                should_resume = current is target and row["ticket_status"] in {
                    TicketStatus.PROCESSING.value,
                    TicketStatus.FAILED.value,
                }
                if should_resume and row["ticket_status"] == TicketStatus.FAILED.value:
                    await connection.execute(
                        """
                        UPDATE ticketpilot.tickets
                        SET status = %s, version = version + 1, updated_at = now()
                        WHERE tenant_id = %s AND id = %s
                        """,
                        (
                            TicketStatus.PROCESSING.value,
                            principal.tenant_id,
                            row["ticket_id"],
                        ),
                    )
                await self._append_audit(
                    connection,
                    principal.tenant_id,
                    row["ticket_id"],
                    run_id,
                    "APPROVAL_DECISION_REPLAYED",
                    AuditOutcome.SUCCEEDED,
                    actor_type=ActorType.STAFF,
                    actor_id=principal.actor_id,
                    approval_id=approval_id,
                    node_name="approval_api",
                    details={
                        "decision": decision.value,
                        "current_status": current.value,
                        "resume_required": should_resume,
                    },
                )
                return ApprovalDecisionResult(
                    approval_id=approval_id,
                    ticket_id=row["ticket_id"],
                    thread_id=row["thread_id"],
                    status=current,
                    should_resume=should_resume,
                )
            if current is not ApprovalStatus.PENDING:
                raise StateConflict(
                    f"Approval is already {current.value}; it cannot become {target.value}"
                )

            await connection.execute(
                """
                UPDATE ticketpilot.approvals
                SET status = %s, decided_by = %s, decision_reason = %s, decided_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (target.value, principal.actor_id, reason, principal.tenant_id, approval_id),
            )
            cursor = await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET status = %s, version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s AND status = %s
                """,
                (
                    TicketStatus.PROCESSING.value,
                    principal.tenant_id,
                    row["ticket_id"],
                    TicketStatus.WAITING_APPROVAL.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflict("Approval ticket is not waiting for a decision")
            await self._append_audit(
                connection,
                principal.tenant_id,
                row["ticket_id"],
                run_id,
                "APPROVAL_DECIDED",
                AuditOutcome.SUCCEEDED,
                actor_type=ActorType.STAFF,
                actor_id=principal.actor_id,
                approval_id=approval_id,
                node_name="approval_api",
                details={"decision": decision.value, "reason": reason},
            )
        return ApprovalDecisionResult(
            approval_id=approval_id,
            ticket_id=row["ticket_id"],
            thread_id=row["thread_id"],
            status=target,
            should_resume=True,
        )

    async def get_approval_for_resume(self, tenant_id: str, approval_id: UUID) -> dict[str, Any]:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT a.*, t.thread_id, t.status AS ticket_status
                FROM ticketpilot.approvals AS a
                JOIN ticketpilot.tickets AS t
                  ON t.tenant_id = a.tenant_id AND t.id = a.ticket_id
                WHERE a.tenant_id = %s AND a.id = %s
                """,
                (tenant_id, approval_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise ResourceNotFound("Approval")
        return row

    async def execute_approved_refund(
        self, tenant_id: str, approval_id: UUID, run_id: UUID
    ) -> RefundExecutionResult:
        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                SELECT a.status, a.ticket_id, a.action_payload, o.id AS order_id,
                       o.payment_status, o.refundable_amount, o.currency
                FROM ticketpilot.approvals AS a
                JOIN ticketpilot.tickets AS t
                  ON t.tenant_id = a.tenant_id AND t.id = a.ticket_id
                JOIN ticketpilot.orders AS o
                  ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
                WHERE a.tenant_id = %s AND a.id = %s
                FOR UPDATE OF a, t, o
                """,
                (tenant_id, approval_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ResourceNotFound("Approval order")
            if row["status"] == ApprovalStatus.EXECUTED.value:
                return RefundExecutionResult(executed=False, message="Refund was already executed")
            if row["status"] != ApprovalStatus.APPROVED.value:
                raise StateConflict("Refund execution requires an approved approval")

            payload = row["action_payload"]
            amount = Decimal(payload["amount"])
            eligible = (
                payload["order_id"] == str(row["order_id"])
                and payload["currency"] == row["currency"]
                and row["payment_status"]
                in {
                    OrderPaymentStatus.PAID.value,
                    OrderPaymentStatus.PARTIALLY_REFUNDED.value,
                }
                and amount > 0
                and amount <= row["refundable_amount"]
            )
            if not eligible:
                message = "审批后订单状态已变化，Mock 退款被安全阻断。"
                await connection.execute(
                    """
                    UPDATE ticketpilot.approvals
                    SET status = %s
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (ApprovalStatus.CANCELLED.value, tenant_id, approval_id),
                )
                await connection.execute(
                    """
                    UPDATE ticketpilot.tickets
                    SET status = %s, resolution_summary = %s,
                        version = version + 1, updated_at = now()
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (TicketStatus.FAILED.value, message, tenant_id, row["ticket_id"]),
                )
                await self._append_agent_message(
                    connection, tenant_id, row["ticket_id"], run_id, message, []
                )
                await self._append_audit(
                    connection,
                    tenant_id,
                    row["ticket_id"],
                    run_id,
                    "REFUND_EXECUTION_BLOCKED",
                    AuditOutcome.BLOCKED,
                    actor_type=ActorType.SYSTEM,
                    approval_id=approval_id,
                    node_name="execute_refund_mock",
                    tool_name="execute_refund_mock",
                )
                return RefundExecutionResult(executed=False, message=message)

            remaining = row["refundable_amount"] - amount
            payment_status = (
                OrderPaymentStatus.REFUNDED
                if remaining == 0
                else OrderPaymentStatus.PARTIALLY_REFUNDED
            )
            message = f"退款审批已通过，Mock 退款 {amount:.2f} {row['currency']} 已执行。"
            await connection.execute(
                """
                UPDATE ticketpilot.orders
                SET payment_status = %s, refundable_amount = %s,
                    version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (payment_status.value, remaining, tenant_id, row["order_id"]),
            )
            await connection.execute(
                """
                UPDATE ticketpilot.approvals
                SET status = %s, executed_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (ApprovalStatus.EXECUTED.value, tenant_id, approval_id),
            )
            await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET status = %s, resolution_summary = %s, resolved_at = now(),
                    version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (TicketStatus.RESOLVED.value, message, tenant_id, row["ticket_id"]),
            )
            await self._append_agent_message(
                connection, tenant_id, row["ticket_id"], run_id, message, []
            )
            await self._append_audit(
                connection,
                tenant_id,
                row["ticket_id"],
                run_id,
                "REFUND_EXECUTED",
                AuditOutcome.SUCCEEDED,
                actor_type=ActorType.SYSTEM,
                approval_id=approval_id,
                node_name="execute_refund_mock",
                tool_name="execute_refund_mock",
                details={"amount": str(amount), "remaining_refundable": str(remaining)},
            )
            return RefundExecutionResult(executed=True, message=message)

    async def resolve_rejected_refund(self, tenant_id: str, approval_id: UUID, run_id: UUID) -> str:
        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                SELECT ticket_id, status
                FROM ticketpilot.approvals
                WHERE tenant_id = %s AND id = %s
                FOR UPDATE
                """,
                (tenant_id, approval_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ResourceNotFound("Approval")
            if row["status"] != ApprovalStatus.REJECTED.value:
                raise StateConflict("Refund rejection requires a rejected approval")
            message = "退款申请未获批准，本次未执行任何退款。"
            cursor = await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET status = %s, resolution_summary = %s, resolved_at = now(),
                    version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s AND status = %s
                """,
                (
                    TicketStatus.RESOLVED.value,
                    message,
                    tenant_id,
                    row["ticket_id"],
                    TicketStatus.PROCESSING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflict("Rejected approval ticket is not processing")
            await self._append_agent_message(
                connection, tenant_id, row["ticket_id"], run_id, message, []
            )
            await self._append_audit(
                connection,
                tenant_id,
                row["ticket_id"],
                run_id,
                "REFUND_REJECTED",
                AuditOutcome.SUCCEEDED,
                actor_type=ActorType.SYSTEM,
                approval_id=approval_id,
                node_name="generate_rejection_response",
            )
            return message

    async def fail_run(
        self, tenant_id: str, ticket_id: UUID, run_id: UUID, error_code: str
    ) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE ticketpilot.tickets
                SET status = CASE WHEN status = %s THEN %s ELSE status END,
                    resolution_summary = CASE WHEN status = %s THEN %s ELSE resolution_summary END,
                    version = version + 1, updated_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (
                    TicketStatus.PROCESSING.value,
                    TicketStatus.FAILED.value,
                    TicketStatus.PROCESSING.value,
                    "Agent workflow failed; retry or staff review is required.",
                    tenant_id,
                    ticket_id,
                ),
            )
            await self._append_audit(
                connection,
                tenant_id,
                ticket_id,
                run_id,
                "RUN_FAILED",
                AuditOutcome.FAILED,
                actor_type=ActorType.SYSTEM,
                node_name="handle_failure",
                details={"error_code": error_code},
            )

    async def record_tool_result(
        self,
        tenant_id: str,
        ticket_id: UUID,
        run_id: UUID,
        *,
        tool_name: str,
        succeeded: bool,
        duration_ms: int,
        details: dict[str, Any],
    ) -> None:
        outcome = AuditOutcome.SUCCEEDED if succeeded else AuditOutcome.FAILED
        event_type = "TOOL_SUCCEEDED" if succeeded else "TOOL_FAILED"
        async with self.pool.connection() as connection, connection.transaction():
            await self._append_audit(
                connection,
                tenant_id,
                ticket_id,
                run_id,
                event_type,
                outcome,
                actor_type=ActorType.SYSTEM,
                node_name=tool_name,
                tool_name=tool_name,
                details={**details, "duration_ms": duration_ms},
            )

    @staticmethod
    async def _append_agent_message(
        connection: BusinessConnection,
        tenant_id: str,
        ticket_id: UUID,
        run_id: UUID,
        message: str,
        citations: list[dict[str, Any]],
    ) -> None:
        await connection.execute(
            """
            INSERT INTO ticketpilot.ticket_messages (
                id, tenant_id, ticket_id, run_id, role, content, citations
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                uuid4(),
                tenant_id,
                ticket_id,
                run_id,
                MessageRole.AGENT.value,
                message,
                Jsonb(citations),
            ),
        )

    @staticmethod
    async def _append_audit(
        connection: BusinessConnection,
        tenant_id: str,
        ticket_id: UUID,
        run_id: UUID,
        event_type: str,
        outcome: AuditOutcome,
        *,
        actor_type: ActorType,
        actor_id: str | None = None,
        approval_id: UUID | None = None,
        node_name: str | None = None,
        tool_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO ticketpilot.audit_events (
                id, tenant_id, ticket_id, run_id, approval_id,
                actor_type, actor_id, event_type, node_name, tool_name,
                outcome, details
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                uuid4(),
                tenant_id,
                ticket_id,
                run_id,
                approval_id,
                actor_type.value,
                actor_id,
                event_type,
                node_name,
                tool_name,
                outcome.value,
                Jsonb(details or {}),
            ),
        )
