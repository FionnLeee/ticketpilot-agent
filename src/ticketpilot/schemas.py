from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from ticketpilot.domain import (
    ActorType,
    ApprovalDecision,
    ApprovalStatus,
    AuditOutcome,
    FulfillmentStatus,
    MessageRole,
    OrderLookupErrorCode,
    OrderPaymentStatus,
    PolicyLookupErrorCode,
    PrincipalRole,
    ProcessingResult,
    RiskLevel,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)


class TicketPilotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RequestPrincipal(TicketPilotModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    role: PrincipalRole


class CreateTicketRequest(TicketPilotModel):
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8000)
    order_reference: str | None = Field(default=None, min_length=1, max_length=128)


class AddTicketMessageRequest(TicketPilotModel):
    message: str = Field(min_length=1, max_length=8000)


class ApprovalDecisionRequest(TicketPilotModel):
    decision: ApprovalDecision
    reason: str = Field(min_length=1, max_length=1000)


class Citation(TicketPilotModel):
    source_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=500)
    uri: HttpUrl | None = None
    chunk_id: str | None = Field(default=None, max_length=256)
    excerpt: str | None = Field(default=None, max_length=1500)


class OrderSummary(TicketPilotModel):
    order_reference: str = Field(min_length=1, max_length=128)
    payment_status: OrderPaymentStatus
    fulfillment_status: FulfillmentStatus
    paid_amount: Decimal = Field(ge=Decimal("0"), max_digits=12, decimal_places=2)
    refundable_amount: Decimal = Field(ge=Decimal("0"), max_digits=12, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    carrier: str | None = Field(default=None, max_length=200)
    tracking_number_masked: str | None = Field(default=None, max_length=200)
    estimated_delivery_at: datetime | None = None

    @model_validator(mode="after")
    def refundable_amount_cannot_exceed_paid_amount(self) -> "OrderSummary":
        if self.refundable_amount > self.paid_amount:
            raise ValueError("refundable_amount cannot exceed paid_amount")
        return self


class OrderLookupResult(TicketPilotModel):
    found: bool
    order_reference: str = Field(min_length=1, max_length=128)
    order: OrderSummary | None = None
    error_code: OrderLookupErrorCode | None = None

    @model_validator(mode="after")
    def result_shape_matches_found_flag(self) -> "OrderLookupResult":
        if self.found and (self.order is None or self.error_code is not None):
            raise ValueError("A found order requires order data and no error_code")
        if not self.found and (self.order is not None or self.error_code is None):
            raise ValueError("A failed lookup requires error_code and no order data")
        return self


class PolicySearchResult(TicketPilotModel):
    query: str = Field(min_length=1, max_length=1000)
    evidence: list[Citation] = Field(default_factory=list)
    error_code: PolicyLookupErrorCode | None = None

    @model_validator(mode="after")
    def evidence_matches_error(self) -> "PolicySearchResult":
        if self.evidence and self.error_code is not None:
            raise ValueError("Policy evidence and error_code are mutually exclusive")
        if not self.evidence and self.error_code is None:
            raise ValueError("Empty policy evidence requires an error_code")
        return self


class TicketClassification(TicketPilotModel):
    full_refund_requested: bool = False
    category: TicketCategory
    priority: TicketPriority = TicketPriority.NORMAL
    order_reference: str | None = Field(default=None, min_length=1, max_length=128)
    requested_refund_amount: Decimal | None = Field(
        default=None,
        gt=Decimal("0"),
        max_digits=12,
        decimal_places=2,
    )


class TicketMessageView(TicketPilotModel):
    id: UUID
    run_id: UUID | None = None
    role: MessageRole
    content: str
    citations: list[Citation] = Field(default_factory=list)
    created_at: datetime


class ApprovalSummary(TicketPilotModel):
    id: UUID
    action_type: str
    status: ApprovalStatus
    action_payload: dict[str, Any]
    requested_at: datetime
    decided_by: str | None = None
    decision_reason: str | None = None
    decided_at: datetime | None = None
    executed_at: datetime | None = None


class TicketSummary(TicketPilotModel):
    id: UUID
    thread_id: str
    status: TicketStatus
    processing_result: ProcessingResult | None = None
    subject: str
    category: TicketCategory | None = None
    priority: TicketPriority | None = None
    risk_level: RiskLevel | None = None
    order_reference: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def processing_result_matches_status(self) -> "TicketSummary":
        allowed = {
            TicketStatus.NEW: {None},
            TicketStatus.PROCESSING: {None},
            TicketStatus.WAITING_APPROVAL: {ProcessingResult.WAITING_APPROVAL},
            TicketStatus.WAITING_INFORMATION: {ProcessingResult.NEEDS_INPUT},
            TicketStatus.RESOLVED: {ProcessingResult.ANSWERED},
            TicketStatus.FAILED: {
                ProcessingResult.DEPENDENCY_FAILED,
                ProcessingResult.INSUFFICIENT_EVIDENCE,
                ProcessingResult.PROCESSING_FAILED,
            },
        }
        if self.processing_result not in allowed[self.status]:
            raise ValueError("processing_result does not match ticket status")
        return self


class TicketDetail(TicketSummary):
    resolution_summary: str | None = None
    order: OrderSummary | None = None
    messages: list[TicketMessageView] = Field(default_factory=list)
    pending_approval: ApprovalSummary | None = None


class TicketRunResult(TicketPilotModel):
    ticket: TicketSummary
    run_id: UUID
    latest_message: TicketMessageView | None = None
    pending_approval: ApprovalSummary | None = None


class AuditEventView(TicketPilotModel):
    id: UUID
    ticket_id: UUID | None = None
    run_id: UUID | None = None
    approval_id: UUID | None = None
    actor_type: ActorType
    actor_id: str | None = None
    event_type: str
    node_name: str | None = None
    tool_name: str | None = None
    outcome: AuditOutcome
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class RunEventsResponse(TicketPilotModel):
    run_id: UUID
    events: list[AuditEventView] = Field(default_factory=list)


class ApiError(TicketPilotModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(TicketPilotModel):
    error: ApiError
