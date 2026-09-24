import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from ticketpilot.domain import PrincipalRole, ProcessingResult
from ticketpilot.errors import Forbidden, ResourceNotFound, TicketPilotUnavailable
from ticketpilot.graph import TicketGraphContext
from ticketpilot.observability import (
    ExecutionLimitExceeded,
    ExecutionLimits,
    ModelCallFailed,
    RunTelemetry,
    current_run,
)
from ticketpilot.orders import OrderReader
from ticketpilot.paths import find_ancestor_path
from ticketpilot.policies import PolicyRetriever
from ticketpilot.privacy import mask_tracking_number
from ticketpilot.reasoning import TicketReasoner
from ticketpilot.repositories import CreatedTicket, TicketRepository
from ticketpilot.schemas import (
    AddTicketMessageRequest,
    ApprovalDecisionRequest,
    ApprovalListResponse,
    ApprovalQueueItem,
    ApprovalSummary,
    AuditEventView,
    CreateTicketRequest,
    DashboardSummary,
    DatasetEvidence,
    OrderSummary,
    RequestPrincipal,
    RunEventsResponse,
    TicketDetail,
    TicketListResponse,
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
        execution_limits: ExecutionLimits | None = None,
    ) -> None:
        self.repository = repository
        self.workflow = workflow
        self.workflow_repository = workflow_repository
        self.order_reader = order_reader
        self.policy_retriever = policy_retriever
        self.reasoner = reasoner
        self.execution_limits = execution_limits or ExecutionLimits()

    async def _invoke_owned(
        self,
        principal: RequestPrincipal,
        ticket_id: UUID,
        run_id: UUID,
        thread_id: str,
        graph_input: Any,
    ) -> TicketRunResult:
        context = self._workflow_context(principal)
        workflow = self.workflow
        if workflow is None:
            raise TicketPilotUnavailable
        await self.repository.claim_run(principal.tenant_id, ticket_id, run_id)
        telemetry = RunTelemetry(str(run_id), self.execution_limits)
        token = current_run.set(telemetry)
        try:
            async with asyncio.timeout(self.execution_limits.deadline_seconds):
                await workflow.ainvoke(
                    graph_input,
                    config=RunnableConfig(configurable={"thread_id": thread_id}),
                    context=context,
                )
            return await self._run_result(principal, ticket_id, run_id)
        except (ModelCallFailed, ExecutionLimitExceeded) as exc:
            await context.workflow_repository.record_unsuccessful_result(
                principal.tenant_id,
                ticket_id,
                run_id,
                "模型服务暂时不可用或本轮执行预算已用尽，请稍后重新提交或转人工核查。",
                ProcessingResult.DEPENDENCY_FAILED,
                type(exc).__name__,
            )
            return await self._run_result(principal, ticket_id, run_id)
        except BaseException as exc:
            await context.workflow_repository.fail_run(
                principal.tenant_id,
                ticket_id,
                run_id,
                type(exc).__name__,
            )
            raise
        finally:
            current_run.reset(token)
            try:
                await context.workflow_repository.record_runtime_events(
                    principal.tenant_id,
                    ticket_id,
                    run_id,
                    telemetry.events,
                    telemetry.summary(),
                )
            finally:
                await self.repository.release_run(principal.tenant_id, ticket_id, run_id)

    async def create_ticket(
        self,
        principal: RequestPrincipal,
        request: CreateTicketRequest,
        idempotency_key: str,
    ) -> TicketRunResult:
        if principal.role not in CREATE_TICKET_ROLES:
            raise Forbidden
        if self.workflow is not None:
            self._workflow_context(principal)
        created = await self.repository.create(
            principal,
            request,
            idempotency_key,
            reserve_run=self.workflow is not None,
        )
        if self.workflow is not None and created.should_run:
            return await self._invoke_owned(
                principal,
                created.ticket["id"],
                created.run_id,
                created.ticket["thread_id"],
                {
                    "messages": [],
                    "ticket_id": str(created.ticket["id"]),
                    "run_id": str(created.run_id),
                },
            )
        if self.workflow is not None:
            return await self._run_result(principal, created.ticket["id"], created.run_id)
        return self._created_ticket_result(created, request.order_reference)

    async def decide_approval(
        self,
        principal: RequestPrincipal,
        approval_id: UUID,
        request: ApprovalDecisionRequest,
    ) -> TicketRunResult:
        context = self._workflow_context(principal)
        run_id = uuid4()
        decision = await context.workflow_repository.decide_approval(
            principal,
            approval_id,
            request.decision,
            request.reason,
            run_id,
        )
        if decision.should_resume:
            return await self._invoke_owned(
                principal,
                decision.ticket_id,
                run_id,
                decision.thread_id,
                Command(resume={"approval_id": str(approval_id)}, update={"run_id": str(run_id)}),
            )
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
        self._workflow_context(principal)
        appended = await self.repository.append_message(
            principal, ticket_id, request, idempotency_key
        )
        if appended.should_run:
            return await self._invoke_owned(
                principal,
                ticket_id,
                appended.run_id,
                appended.thread_id,
                {"messages": [], "ticket_id": str(ticket_id), "run_id": str(appended.run_id)},
            )
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

    async def list_tickets(
        self, principal: RequestPrincipal, limit: int = 40
    ) -> TicketListResponse:
        if principal.role not in READ_TICKET_ROLES:
            raise Forbidden
        rows = await self.repository.list_tickets(principal, limit)
        return TicketListResponse(
            items=[TicketSummary(**self._ticket_summary_data(row)) for row in rows]
        )

    async def list_approvals(
        self, principal: RequestPrincipal, limit: int = 40
    ) -> ApprovalListResponse:
        if principal.role not in {
            PrincipalRole.APPROVER,
            PrincipalRole.STAFF,
            PrincipalRole.ADMIN,
        }:
            raise Forbidden
        rows = await self.repository.list_approvals(principal.tenant_id, limit)
        return ApprovalListResponse(items=[ApprovalQueueItem.model_validate(row) for row in rows])

    async def get_dashboard(self, principal: RequestPrincipal) -> DashboardSummary:
        if principal.role not in READ_TICKET_ROLES:
            raise Forbidden
        data = await self.repository.dashboard_summary(principal.tenant_id)
        tenant = data["tenant"] or {}
        datasets = []
        for row in data["datasets"]:
            counts = {key: int(value) for key, value in row["row_counts"].items() if key != "total"}
            datasets.append(
                DatasetEvidence(
                    dataset_id=row["dataset_id"],
                    loaded_at=row["loaded_at"],
                    evidence_source="POSTGRES_REGISTRY",
                    row_counts=counts,
                    total_rows=int(row["row_counts"].get("total", sum(counts.values()))),
                )
            )
        if not datasets:
            benchmark_path = find_ancestor_path(
                Path(__file__), "data", "ticketpilot", "history_benchmark.json"
            )
            if benchmark_path.exists():
                benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
                counts = {
                    key: int(value)
                    for key, value in benchmark["row_counts"].items()
                    if key != "total"
                }
                datasets.append(
                    DatasetEvidence(
                        dataset_id=benchmark["dataset_id"],
                        loaded_at=benchmark["measured_at"],
                        evidence_source="VERSIONED_BENCHMARK",
                        row_counts=counts,
                        total_rows=int(benchmark["row_counts"]["total"]),
                    )
                )
        return DashboardSummary(
            tenant_id=principal.tenant_id,
            order_count=tenant.get("order_count", 0),
            customer_count=tenant.get("customer_count", 0),
            ticket_count=tenant.get("ticket_count", 0),
            open_ticket_count=tenant.get("open_ticket_count", 0),
            refund_ticket_count=tenant.get("refund_ticket_count", 0),
            contact_rate=tenant.get("contact_rate", 0),
            avg_resolution_minutes=tenant.get("avg_resolution_minutes"),
            datasets=datasets,
            daily_volume=data["daily_volume"],
            refund_funnel=data["refund_funnel"],
        )

    async def get_run_events(self, principal: RequestPrincipal, run_id: UUID) -> RunEventsResponse:
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
                processing_result=detail.processing_result,
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
            "processing_result": ticket.get("processing_result"),
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
