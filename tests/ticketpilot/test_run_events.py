from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from ticketpilot.domain import ActorType, AuditOutcome, PrincipalRole
from ticketpilot.errors import Forbidden, ResourceNotFound
from ticketpilot.repositories import TicketRepository
from ticketpilot.schemas import RequestPrincipal
from ticketpilot.services import TicketService


class FakeEventRepository:
    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.calls = []

    async def list_run_events(self, principal, run_id):
        self.calls.append((principal, run_id))
        return self.events


def make_principal(role: PrincipalRole = PrincipalRole.CUSTOMER) -> RequestPrincipal:
    return RequestPrincipal(tenant_id="tenant-a", actor_id="customer-a", role=role)


def make_event(run_id):
    return {
        "id": uuid4(),
        "ticket_id": uuid4(),
        "run_id": run_id,
        "approval_id": None,
        "actor_type": ActorType.CUSTOMER,
        "actor_id": "customer-a",
        "event_type": "TICKET_CREATED",
        "node_name": None,
        "tool_name": None,
        "outcome": AuditOutcome.SUCCEEDED,
        "details": {},
        "occurred_at": datetime.now(UTC),
    }


@pytest.mark.asyncio
async def test_get_run_events_returns_typed_timeline() -> None:
    run_id = uuid4()
    repository = FakeEventRepository([make_event(run_id)])
    service = TicketService(cast(TicketRepository, repository))

    result = await service.get_run_events(make_principal(), run_id)

    assert result.run_id == run_id
    assert result.events[0].event_type == "TICKET_CREATED"
    assert repository.calls[0][0].tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_get_run_events_hides_missing_or_out_of_scope_run() -> None:
    service = TicketService(cast(TicketRepository, FakeEventRepository([])))

    with pytest.raises(ResourceNotFound):
        await service.get_run_events(make_principal(), uuid4())


@pytest.mark.asyncio
async def test_get_run_events_rejects_agent_role_before_repository_access() -> None:
    repository = FakeEventRepository([])
    service = TicketService(cast(TicketRepository, repository))

    with pytest.raises(Forbidden):
        await service.get_run_events(make_principal(PrincipalRole.AGENT), uuid4())

    assert repository.calls == []
