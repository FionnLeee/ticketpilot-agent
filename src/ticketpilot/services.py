from typing import Any
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from ticketpilot.domain import PrincipalRole
from ticketpilot.errors import Forbidden, ResourceNotFound, TicketPilotUnavailable
from ticketpilot.graph import TicketGraphContext
from ticketpilot.orders import OrderReader
from ticketpilot.policies import PolicyRetriever
from ticketpilot.privacy import mask_tracking_number
from ticketpilot.reasoning import TicketReasoner
from ticketpilot.repositories import CreatedTicket, TicketRepository
from ticketpilot.schemas import (
    AddTicketMessageRequest,
    ApprovalDecisionRequest,
    ApprovalSummary,
    AuditEventView,
    CreateTicketRequest,
    OrderSummary,
    RequestPrincipal,
    RunEventsResponse,
    TicketDetail,
    TicketMessageView,
    TicketRunResult,
    TicketSummary,
)
from ticketpilot.workflow_repository import TicketWorkflowRepository

CREATE_TICKET_ROLES = {
    PrincipalRole.CUSTOMER,
    PrincipalRole.STAFF,
    PrincipalRole.ADMIN,
}
READ_TICKET_ROLES = {
    PrincipalRole.CUSTOMER,
    PrincipalRole.STAFF,
    PrincipalRole.APPROVER,
    PrincipalRole.ADMIN,
}


class TicketService:
    def __init__(
        self,
        repository: TicketRepository,
        *,
        workflow: Any | None = None,
        workflow_repository: TicketWorkflowRepository | None = None,
        order_reader: OrderReader | None = None,
        policy_retriever: PolicyRetriever | None = None,
        reasoner: TicketReasoner | None = None,
    ) -> None:
        self.repository = repository
        self.workflow = workflow
        self.workflow_repository = workflow_repository
        self.order_reader = order_reader
        self.policy_retriever = policy_retriever
        self.reasoner = reasoner

    async def create_ticket(
        self, principal: RequestPrincipal, request: CreateTicketRequest
    ) -> TicketRunResult:
        if principal.role not in CREATE_TICKET_ROLES:
            raise Forbidden

        created = await self.repository.create(principal, request)
        if self.workflow is not None:
            context = self._workflow_context(principal)
            try:
                await self.workflow.ainvoke(
                    {
                        "messages": [],
                        "ticket_id": str(created.ticket["id"]),
                        "run_id": str(created.run_id),
                    },
                    config=RunnableConfig(configurable={"thread_id": created.ticket["thread_id"]}),
                    context=context,
                )
            except Exception as exc:
                await context.workflow_repository.fail_run(
                    principal.tenant_id,
                    created.ticket["id"],
                    created.run_id,
                    type(exc).__name__,
                )
                raise
            return await self._run_result(principal, created.ticket["id"], created.run_id)
        return self._created_ticket_result(created, request.order_reference)

    async def decide_approval(
        self,
        principal: RequestPrincipal,
        approval_id: UUID,
        request: ApprovalDecisionRequest,
    ) -> TicketRunResult:
        context = self._workflow_context(principal)
        workflow = self.workflow
        if workflow is None:
            raise TicketPilotUnavailable
        run_id = uuid4()
        decision = await context.workflow_repository.decide_approval(
            principal,
            approval_id,
            request.decision,
            request.reason,
            run_id,
        )
        if decision.should_resume:
            try:
                await workflow.ainvoke(
                    Command(
                        resume={"approval_id": str(approval_id)},
                        update={"run_id": str(run_id)},
                    ),
                    config=RunnableConfig(configurable={"thread_id": decision.thread_id}),
                    context=context,
                )
            except Exception as exc:
                await context.workflow_repository.fail_run(
                    principal.tenant_id,
                    decision.ticket_id,
                    run_id,
                    type(exc).__name__,
                )
                raise
        return await self._run_result(principal, decision.ticket_id, run_id)

    async def add_message(
        self,
        principal: RequestPrincipal,
        ticket_id: UUID,
        request: AddTicketMessageRequest,
        idempotency_key: str,
    ) -> TicketRunResult:
        if principal.role not in CREATE_TICKET_ROLES:
            raise Forbidden
        if self.workflow is None:
            raise TicketPilotUnavailable

        appended = await self.repository.append_message(
            principal, ticket_id, request, idempotency_key
        )
        if appended.should_run:
            context = self._workflow_context(principal)
            try:
                await self.workflow.ainvoke(
                    {
                        "messages": [],
                        "ticket_id": str(ticket_id),
                        "run_id": str(appended.run_id),
                    },
                    config=RunnableConfig(configurable={"thread_id": appended.thread_id}),
                    context=context,
                )
            except Exception as exc:
                await context.workflow_repository.fail_run(
                    principal.tenant_id,
                    ticket_id,
                    appended.run_id,
                    type(exc).__name__,
                )
                raise
        return await self._run_result(principal, ticket_id, appended.run_id)

    async def get_ticket(self, principal: RequestPrincipal, ticket_id: UUID) -> TicketDetail:
        if principal.role not in READ_TICKET_ROLES:
            raise Forbidden

        ticket = await self.repository.get_ticket(principal, ticket_id)
        if ticket is None:
            raise ResourceNotFound("Ticket")

        messages = await self.repository.list_messages(principal.tenant_id, ticket_id)
        pending_approval = await self.repository.get_pending_approval(
            principal.tenant_id, ticket_id
        )
        return TicketDetail(
            **self._ticket_summary_data(ticket),
            resolution_summary=ticket["resolution_summary"],
            order=self._order_summary(ticket),
            messages=[TicketMessageView.model_validate(row) for row in messages],
            pending_approval=(
                ApprovalSummary.model_validate(pending_approval) if pending_approval else None
            ),
        )

    async def get_run_events(
        self, principal: RequestPrincipal, run_id: UUID
    ) -> RunEventsResponse:
        if principal.role not in READ_TICKET_ROLES:
            raise Forbidden

        events = await self.repository.list_run_events(principal, run_id)
        if not events:
            raise ResourceNotFound("Run")
        return RunEventsResponse(
            run_id=run_id,
            events=[AuditEventView.model_validate(event) for event in events],
        )

    def _workflow_context(self, principal: RequestPrincipal) -> TicketGraphContext:
        if (
            self.workflow is None
            or self.workflow_repository is None
            or self.order_reader is None
            or self.policy_retriever is None
            or self.reasoner is None
        ):
            raise TicketPilotUnavailable
        return TicketGraphContext(
            principal=principal,
            workflow_repository=self.workflow_repository,
            order_reader=self.order_reader,
            policy_retriever=self.policy_retriever,
            reasoner=self.reasoner,
        )

    async def _run_result(
        self, principal: RequestPrincipal, ticket_id: UUID, run_id: UUID
    ) -> TicketRunResult:
        detail = await self.get_ticket(principal, ticket_id)
        return TicketRunResult(
            ticket=TicketSummary(
                id=detail.id,
                thread_id=detail.thread_id,
                status=detail.status,
                subject=detail.subject,
                category=detail.category,
                priority=detail.priority,
                risk_level=detail.risk_level,
                order_reference=detail.order_reference,
                created_at=detail.created_at,
                updated_at=detail.updated_at,
            ),
            run_id=run_id,
            latest_message=detail.messages[-1] if detail.messages else None,
            pending_approval=detail.pending_approval,
        )

    @classmethod
    def _created_ticket_result(
        cls, created: CreatedTicket, order_reference: str | None
    ) -> TicketRunResult:
        ticket_data = cls._ticket_summary_data(created.ticket)
        ticket_data["order_reference"] = order_reference
        return TicketRunResult(
            ticket=TicketSummary(**ticket_data),
            run_id=created.run_id,
            latest_message=TicketMessageView.model_validate(created.message),
        )

    @staticmethod
    def _ticket_summary_data(ticket: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": ticket["id"],
            "thread_id": ticket["thread_id"],
            "status": ticket["status"],
            "subject": ticket["subject"],
            "category": ticket["category"],
            "priority": ticket["priority"],
            "risk_level": ticket["risk_level"],
            "order_reference": ticket.get("order_reference"),
            "created_at": ticket["created_at"],
            "updated_at": ticket["updated_at"],
        }

    @staticmethod
    def _order_summary(ticket: dict[str, Any]) -> OrderSummary | None:
        if ticket.get("order_reference") is None:
            return None
        return OrderSummary(
            order_reference=ticket["order_reference"],
            payment_status=ticket["payment_status"],
            fulfillment_status=ticket["fulfillment_status"],
            paid_amount=ticket["paid_amount"],
            refundable_amount=ticket["refundable_amount"],
            currency=ticket["currency"],
            carrier=ticket["carrier"],
            tracking_number_masked=mask_tracking_number(ticket["tracking_number"]),
            estimated_delivery_at=ticket["estimated_delivery_at"],
        )
