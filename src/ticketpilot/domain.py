from enum import StrEnum


class TicketStatus(StrEnum):
    NEW = "NEW"
    PROCESSING = "PROCESSING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"


class TicketCategory(StrEnum):
    ORDER_STATUS = "ORDER_STATUS"
    REFUND = "REFUND"
    POLICY = "POLICY"
    OTHER = "OTHER"


class TicketPriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class RiskLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    LOW_RISK_WRITE = "LOW_RISK_WRITE"
    HIGH_RISK_WRITE = "HIGH_RISK_WRITE"


class OrderPaymentStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"


class FulfillmentStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class OrderLookupErrorCode(StrEnum):
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"


class PolicyLookupErrorCode(StrEnum):
    NO_RELEVANT_POLICY = "NO_RELEVANT_POLICY"
    DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class ApprovalDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class MessageRole(StrEnum):
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    STAFF = "STAFF"


class ActorType(StrEnum):
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    STAFF = "STAFF"
    SYSTEM = "SYSTEM"


class PrincipalRole(StrEnum):
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    STAFF = "STAFF"
    APPROVER = "APPROVER"
    ADMIN = "ADMIN"


class AuditOutcome(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


ALLOWED_TICKET_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.NEW: frozenset({TicketStatus.PROCESSING}),
    TicketStatus.PROCESSING: frozenset(
        {
            TicketStatus.WAITING_APPROVAL,
            TicketStatus.RESOLVED,
            TicketStatus.FAILED,
        }
    ),
    TicketStatus.WAITING_APPROVAL: frozenset({TicketStatus.PROCESSING}),
    TicketStatus.RESOLVED: frozenset({TicketStatus.PROCESSING}),
    TicketStatus.FAILED: frozenset({TicketStatus.PROCESSING}),
}


class InvalidTicketTransition(ValueError):
    def __init__(self, current: TicketStatus, target: TicketStatus) -> None:
        super().__init__(f"Ticket cannot transition from {current.value} to {target.value}")
        self.current = current
        self.target = target


def can_transition_ticket(current: TicketStatus, target: TicketStatus) -> bool:
    return target in ALLOWED_TICKET_TRANSITIONS[current]


def ensure_ticket_transition(current: TicketStatus, target: TicketStatus) -> None:
    if not can_transition_ticket(current, target):
        raise InvalidTicketTransition(current, target)
