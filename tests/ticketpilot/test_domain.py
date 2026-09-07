import pytest

from ticketpilot.domain import (
    ALLOWED_TICKET_TRANSITIONS,
    InvalidTicketTransition,
    TicketStatus,
    can_transition_ticket,
    ensure_ticket_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TicketStatus.NEW, TicketStatus.PROCESSING),
        (TicketStatus.PROCESSING, TicketStatus.WAITING_APPROVAL),
        (TicketStatus.PROCESSING, TicketStatus.RESOLVED),
        (TicketStatus.PROCESSING, TicketStatus.FAILED),
        (TicketStatus.WAITING_APPROVAL, TicketStatus.PROCESSING),
        (TicketStatus.RESOLVED, TicketStatus.PROCESSING),
        (TicketStatus.FAILED, TicketStatus.PROCESSING),
    ],
)
def test_allowed_ticket_transitions(current: TicketStatus, target: TicketStatus) -> None:
    assert can_transition_ticket(current, target)
    ensure_ticket_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TicketStatus.NEW, TicketStatus.RESOLVED),
        (TicketStatus.NEW, TicketStatus.WAITING_APPROVAL),
        (TicketStatus.WAITING_APPROVAL, TicketStatus.RESOLVED),
        (TicketStatus.RESOLVED, TicketStatus.FAILED),
        (TicketStatus.PROCESSING, TicketStatus.PROCESSING),
    ],
)
def test_rejected_ticket_transitions(current: TicketStatus, target: TicketStatus) -> None:
    assert not can_transition_ticket(current, target)
    with pytest.raises(InvalidTicketTransition) as exc_info:
        ensure_ticket_transition(current, target)
    assert exc_info.value.current is current
    assert exc_info.value.target is target


def test_every_status_has_an_explicit_transition_policy() -> None:
    assert set(ALLOWED_TICKET_TRANSITIONS) == set(TicketStatus)
