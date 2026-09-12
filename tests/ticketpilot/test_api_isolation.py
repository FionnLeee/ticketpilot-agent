import asyncio
import sys
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver

from service import service
from ticketpilot.api import get_ticket_service
from ticketpilot.db import apply_migrations, get_ticketpilot_pool
from ticketpilot.graph import build_ticketpilot_graph
from ticketpilot.orders import PostgresOrderRepository
from ticketpilot.policies import LocalPolicyRetriever
from ticketpilot.reasoning import DeterministicDemoReasoner
from ticketpilot.repositories import TicketRepository
from ticketpilot.services import TicketService
from ticketpilot.workflow_repository import TicketWorkflowRepository

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.mark.docker
@pytest.mark.asyncio
async def test_business_http_authorization_with_real_repository(monkeypatch):
    tenant = f"isolation-{uuid4()}"
    monkeypatch.setattr(service.settings, "TICKETPILOT_ENABLED", True)
    monkeypatch.setattr(service.settings, "AUTH_SECRET", None)
    monkeypatch.setattr(
        service.settings,
        "TICKETPILOT_AUTH_TOKENS",
        {
            "owner": {"tenant_id": tenant, "actor_id": "customer-a", "role": "CUSTOMER"},
            "other-customer": {"tenant_id": tenant, "actor_id": "customer-b", "role": "CUSTOMER"},
            "other-tenant": {
                "tenant_id": f"{tenant}-other",
                "actor_id": "customer-a",
                "role": "CUSTOMER",
            },
        },
    )
    app = service.create_app()
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        business = TicketService(
            TicketRepository(pool),
            workflow=build_ticketpilot_graph(checkpointer=MemorySaver()),
            workflow_repository=TicketWorkflowRepository(pool),
            order_reader=PostgresOrderRepository(pool),
            policy_retriever=LocalPolicyRetriever(),
            reasoner=DeterministicDemoReasoner(),
        )
        app.dependency_overrides[get_ticket_service] = lambda: business
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                owner = {
                    "Authorization": "Bearer owner",
                    "Idempotency-Key": "create-isolation",
                }
                created = await client.post(
                    "/v1/tickets",
                    headers=owner,
                    json={
                        "subject": "权限回归",
                        "message": "SYNTHETIC_OWNER_PRIVATE_MESSAGE",
                    },
                )
                assert created.status_code == 201
                result = created.json()
                ticket_path = f"/v1/tickets/{result['ticket']['id']}"
                events_path = f"/v1/runs/{result['run_id']}/events"
                before = await client.get(ticket_path, headers=owner)
                assert before.status_code == 200
                assert "SYNTHETIC_OWNER_PRIVATE_MESSAGE" in before.text
                before_events = await client.get(events_path, headers=owner)
                assert before_events.status_code == 200

                for token, expected in [
                    (None, 401),
                    ("other-customer", 404),
                    ("other-tenant", 404),
                ]:
                    headers = {"Authorization": f"Bearer {token}"} if token else {}
                    for path in [ticket_path, events_path]:
                        response = await client.get(path, headers=headers)
                        assert response.status_code == expected
                        assert "SYNTHETIC_OWNER_PRIVATE_MESSAGE" not in response.text
                    response = await client.post(
                        f"{ticket_path}/messages",
                        headers={
                            **headers,
                            "Idempotency-Key": f"blocked-{token}",
                        },
                        json={"message": "unauthorized overwrite"},
                    )
                    assert response.status_code == expected

                after = await client.get(ticket_path, headers=owner)
                after_events = await client.get(events_path, headers=owner)
                assert after.json() == before.json()
                assert after_events.json() == before_events.json()
        finally:
            async with pool.connection() as connection:
                await connection.execute(
                    "DELETE FROM ticketpilot.audit_events WHERE tenant_id = %s", (tenant,)
                )
                await connection.execute(
                    "DELETE FROM ticketpilot.ticket_messages WHERE tenant_id = %s", (tenant,)
                )
                await connection.execute(
                    "DELETE FROM ticketpilot.tickets WHERE tenant_id = %s", (tenant,)
                )
