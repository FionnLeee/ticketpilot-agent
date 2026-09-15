from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import SecretStr

from service import service

GENERIC_ROUTES = [
    ("POST", "/invoke"),
    ("POST", "/chatbot/invoke"),
    ("POST", "/stream"),
    ("POST", "/chatbot/stream"),
    ("POST", "/history"),
    ("POST", "/chatbot/history"),
    ("GET", "/threads"),
    ("GET", "/chatbot/threads"),
    ("POST", "/feedback"),
    ("POST", "/agui/run"),
    ("POST", "/agui/chatbot/run"),
]


@pytest.mark.parametrize("bearer", [None, "other-customer", "other-tenant", "generic-secret"])
def test_ticketpilot_has_no_generic_entry_points(monkeypatch, bearer):
    monkeypatch.setattr(service.settings, "TICKETPILOT_ENABLED", True)
    monkeypatch.setattr(service.settings, "AUTH_SECRET", SecretStr("generic-secret"))
    monkeypatch.setattr(
        service.settings,
        "TICKETPILOT_AUTH_TOKENS",
        {
            "other-customer": {
                "tenant_id": "tenant-a",
                "actor_id": "customer-b",
                "role": "CUSTOMER",
            },
            "other-tenant": {"tenant_id": "tenant-b", "actor_id": "customer-a", "role": "CUSTOMER"},
        },
    )
    get_agent = Mock(side_effect=AssertionError("Generic agent must not be accessed"))
    monkeypatch.setattr(service, "get_agent", get_agent)
    app = service.create_app()
    assert set(app.openapi()["paths"]) == {
        "/info",
            "/health",
            "/v1/me",
            "/v1/dashboard/summary",
            "/v1/tickets",
            "/v1/tickets/{ticket_id}",
            "/v1/tickets/{ticket_id}/messages",
            "/v1/runs/{run_id}/events",
            "/v1/approvals",
            "/v1/approvals/{approval_id}:decide",
    }
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    payload = {"thread_id": "known-private-thread", "message": "resume", "user_id": "victim"}
    for method, path in GENERIC_ROUTES:
        response = client.request(method, path, headers=headers, json=payload)
        assert response.status_code == 404, (path, response.text)
        assert path not in app.openapi()["paths"]
    get_agent.assert_not_called()
    assert client.get("/health").status_code == 200
    info = client.get("/info", headers={"Authorization": "Bearer generic-secret"})
    assert info.status_code == 200
    assert info.json()["ticketpilot_enabled"] is True
    assert info.json()["agents"] == []
    assert info.json()["ticketpilot_reasoner_mode"] in {"llm", "deterministic_demo"}


@pytest.mark.asyncio
async def test_shared_checkpoint_history_reproduction_is_closed(monkeypatch):
    saver = MemorySaver()
    builder = StateGraph(MessagesState)
    builder.add_node("noop", lambda state: {})
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    graph = builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "known-private-thread"}}
    marker = "SYNTHETIC_PRIVATE_TICKET_MESSAGE"
    await graph.ainvoke({"messages": [HumanMessage(content=marker)]}, config)
    before = await saver.aget_tuple(config)
    monkeypatch.setattr(service, "get_agent", lambda _: graph)
    monkeypatch.setattr(service.settings, "AUTH_SECRET", None)

    monkeypatch.setattr(service.settings, "TICKETPILOT_ENABLED", False)
    generic_app = service.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=generic_app), base_url="http://test"
    ) as client:
        response = await client.post("/chatbot/history", json={"thread_id": "known-private-thread"})
        assert response.status_code == 200
        assert marker in response.text

    monkeypatch.setattr(service.settings, "TICKETPILOT_ENABLED", True)
    ticket_app = service.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ticket_app), base_url="http://test"
    ) as client:
        for method, path in GENERIC_ROUTES:
            response = await client.request(
                method, path, json={"thread_id": "known-private-thread", "message": "overwrite"}
            )
            assert response.status_code == 404
            assert marker not in response.text
    after = await saver.aget_tuple(config)
    assert before == after


def test_generic_mode_keeps_existing_routes(monkeypatch):
    monkeypatch.setattr(service.settings, "TICKETPILOT_ENABLED", False)
    monkeypatch.setattr(service.settings, "AUTH_SECRET", None)
    app = service.create_app()
    paths = app.openapi()["paths"]
    for method, path in GENERIC_ROUTES:
        schema_path = path.replace("/chatbot/", "/{agent_id}/")
        assert method.lower() in paths[schema_path]
    info = TestClient(app).get("/info")
    assert info.status_code == 200
    assert info.json()["ticketpilot_enabled"] is False
    assert info.json()["ticketpilot_reasoner_mode"] is None
