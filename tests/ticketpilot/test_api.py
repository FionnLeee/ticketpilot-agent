from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ticketpilot.api import get_ticket_service, install_ticketpilot_api
from ticketpilot.domain import (
    ActorType,
    ApprovalDecision,
    AuditOutcome,
    MessageRole,
    PrincipalRole,
    TicketStatus,
)
from ticketpilot.errors import ResourceNotFound
from ticketpilot.schemas import (
    AddTicketMessageRequest,
    ApprovalDecisionRequest,
    AuditEventView,
    CreateTicketRequest,
    RequestPrincipal,
    RunEventsResponse,
    TicketDetail,
    TicketMessageView,
    TicketRunResult,
    TicketSummary,
)


def make_run_result() -> TicketRunResult:
    now = datetime.now(UTC)
    run_id = uuid4()
    return TicketRunResult(
        ticket=TicketSummary(
            id=uuid4(),
            thread_id=str(uuid4()),
            status=TicketStatus.NEW,
            subject="查询物流",
            order_reference="O-9527",
            created_at=now,
            updated_at=now,
        ),
        run_id=run_id,
        latest_message=TicketMessageView(
            id=uuid4(),
            run_id=run_id,
            role=MessageRole.CUSTOMER,
            content="订单什么时候送到？",
            created_at=now,
        ),
    )


class FakeTicketService:
    def __init__(self, result: TicketRunResult) -> None:
        self.result = result
        self.create_call = None
        self.decision_call = None
        self.message_call = None
        self.events_call = None

    async def create_ticket(
        self, principal: RequestPrincipal, request: CreateTicketRequest
    ) -> TicketRunResult:
        self.create_call = (principal, request)
        return self.result

    async def get_ticket(self, principal: RequestPrincipal, ticket_id) -> TicketDetail:
        if ticket_id != self.result.ticket.id:
            raise ResourceNotFound("Ticket")
        return TicketDetail(**self.result.ticket.model_dump(), messages=[])

    async def add_message(
        self,
        principal: RequestPrincipal,
        ticket_id,
        request: AddTicketMessageRequest,
        idempotency_key: str,
    ) -> TicketRunResult:
        self.message_call = (principal, ticket_id, request, idempotency_key)
        return self.result

    async def decide_approval(
        self,
        principal: RequestPrincipal,
        approval_id,
        request: ApprovalDecisionRequest,
    ) -> TicketRunResult:
        self.decision_call = (principal, approval_id, request)
        return self.result

    async def get_run_events(
        self, principal: RequestPrincipal, run_id
    ) -> RunEventsResponse:
        self.events_call = (principal, run_id)
        return RunEventsResponse(
            run_id=run_id,
            events=[
                AuditEventView(
                    id=uuid4(),
                    ticket_id=self.result.ticket.id,
                    run_id=run_id,
                    actor_type=ActorType.CUSTOMER,
                    actor_id=principal.actor_id,
                    event_type="TICKET_CREATED",
                    outcome=AuditOutcome.SUCCEEDED,
                    occurred_at=datetime.now(UTC),
                )
            ],
        )


def build_client(fake_service: FakeTicketService, monkeypatch) -> TestClient:
    from ticketpilot import auth

    monkeypatch.setattr(
        auth.settings,
        "TICKETPILOT_AUTH_TOKENS",
        {
            "customer-token": {
                "tenant_id": "tenant-a",
                "actor_id": "customer-a",
                "role": PrincipalRole.CUSTOMER.value,
            },
            "approver-token": {
                "tenant_id": "tenant-a",
                "actor_id": "approver-a",
                "role": PrincipalRole.APPROVER.value,
            },
        },
    )
    app = FastAPI()
    install_ticketpilot_api(app)
    app.dependency_overrides[get_ticket_service] = lambda: fake_service
    return TestClient(app)


def test_create_ticket_uses_principal_from_token(monkeypatch) -> None:
    fake_service = FakeTicketService(make_run_result())
    client = build_client(fake_service, monkeypatch)

    response = client.post(
        "/v1/tickets",
        headers={"Authorization": "Bearer customer-token"},
        json={
            "subject": "查询物流",
            "message": "订单什么时候送到？",
            "order_reference": "O-9527",
        },
    )

    assert response.status_code == 201
    assert response.json()["ticket"]["status"] == "NEW"
    principal, request = fake_service.create_call
    assert principal.tenant_id == "tenant-a"
    assert principal.actor_id == "customer-a"
    assert request.order_reference == "O-9527"


def test_ticketpilot_rejects_unknown_bearer_token(monkeypatch) -> None:
    fake_service = FakeTicketService(make_run_result())
    client = build_client(fake_service, monkeypatch)

    response = client.post(
        "/v1/tickets",
        headers={"Authorization": "Bearer attacker-token"},
        json={"subject": "查询物流", "message": "订单在哪里？"},
    )

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "A valid TicketPilot bearer token is required",
            "details": {},
        }
    }


def test_get_ticket_returns_404_without_leaking_scope(monkeypatch) -> None:
    fake_service = FakeTicketService(make_run_result())
    client = build_client(fake_service, monkeypatch)

    response = client.get(
        f"/v1/tickets/{uuid4()}",
        headers={"Authorization": "Bearer customer-token"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_add_message_requires_idempotency_key_and_trusted_principal(monkeypatch) -> None:
    fake_service = FakeTicketService(make_run_result())
    client = build_client(fake_service, monkeypatch)
    ticket_id = fake_service.result.ticket.id

    missing_key = client.post(
        f"/v1/tickets/{ticket_id}/messages",
        headers={"Authorization": "Bearer customer-token"},
        json={"message": "请继续查询"},
    )
    response = client.post(
        f"/v1/tickets/{ticket_id}/messages",
        headers={
            "Authorization": "Bearer customer-token",
            "Idempotency-Key": "message-attempt-1",
        },
        json={"message": "请继续查询"},
    )

    assert missing_key.status_code == 422
    assert response.status_code == 200
    principal, actual_ticket_id, request, key = fake_service.message_call
    assert principal.tenant_id == "tenant-a"
    assert principal.actor_id == "customer-a"
    assert actual_ticket_id == ticket_id
    assert request.message == "请继续查询"
    assert key == "message-attempt-1"


def test_decide_approval_uses_trusted_approver_principal(monkeypatch) -> None:
    fake_service = FakeTicketService(make_run_result())
    client = build_client(fake_service, monkeypatch)
    approval_id = uuid4()

    response = client.post(
        f"/v1/approvals/{approval_id}:decide",
        headers={"Authorization": "Bearer approver-token"},
        json={"decision": "APPROVE", "reason": "reviewed"},
    )

    assert response.status_code == 200
    principal, actual_approval_id, request = fake_service.decision_call
    assert principal.actor_id == "approver-a"
    assert principal.role is PrincipalRole.APPROVER
    assert actual_approval_id == approval_id
    assert request.decision is ApprovalDecision.APPROVE


def test_get_run_events_uses_trusted_tenant_principal(monkeypatch) -> None:
    fake_service = FakeTicketService(make_run_result())
    client = build_client(fake_service, monkeypatch)
    run_id = uuid4()

    response = client.get(
        f"/v1/runs/{run_id}/events",
        headers={"Authorization": "Bearer customer-token"},
    )

    assert response.status_code == 200
    assert response.json()["events"][0]["event_type"] == "TICKET_CREATED"
    principal, actual_run_id = fake_service.events_call
    assert principal.tenant_id == "tenant-a"
    assert principal.actor_id == "customer-a"
    assert actual_run_id == run_id
