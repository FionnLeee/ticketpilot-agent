import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI

from core.settings import DatabaseType
from schema import AgentInfo


@pytest.mark.asyncio
async def test_lifespan(monkeypatch, caplog) -> None:
    """Test that the lifespan sets up the database and store, loads the agents, and logs errors."""
    from service import service

    fake_saver_setup = False
    fake_store_setup = False

    class FakeSaver:
        async def setup(self) -> None:
            nonlocal fake_saver_setup
            fake_saver_setup = True

    class FakeStore:
        async def setup(self) -> None:
            nonlocal fake_store_setup
            fake_store_setup = True

    fake_saver = FakeSaver()
    fake_store = FakeStore()

    @asynccontextmanager
    async def fake_initialize_database():
        yield fake_saver

    @asynccontextmanager
    async def fake_initialize_store():
        yield fake_store

    agents = {
        "good": type("Agent", (), {"checkpointer": None, "store": None})(),
        "bad": type("Agent", (), {"checkpointer": None, "store": None})(),
    }

    async def fake_load_agent(agent_key: str) -> None:
        if agent_key == "bad":
            raise RuntimeError("boom")

    def fake_get_agent(agent_key: str):
        return agents[agent_key]

    monkeypatch.setattr(service, "initialize_database", fake_initialize_database)
    monkeypatch.setattr(service, "initialize_store", fake_initialize_store)
    monkeypatch.setattr(service, "load_agent", fake_load_agent)
    monkeypatch.setattr(service, "get_agent", fake_get_agent)
    monkeypatch.setattr(service.settings, "DATABASE_TYPE", DatabaseType.SQLITE)
    monkeypatch.setattr(service.settings, "TICKETPILOT_ENABLED", False)
    monkeypatch.setattr(
        service,
        "get_all_agent_info",
        lambda: [
            AgentInfo(key="good", description=""),
            AgentInfo(key="bad", description=""),
        ],
    )

    caplog.set_level(logging.INFO, logger=service.logger.name)

    async with service.lifespan(FastAPI()):
        pass

    assert fake_saver_setup
    assert fake_store_setup
    assert agents["good"].checkpointer is fake_saver
    assert agents["good"].store is fake_store
    assert agents["bad"].checkpointer is fake_saver
    assert agents["bad"].store is fake_store

    assert "Agent loaded: good" in caplog.text
    assert "Failed to load agent bad: boom" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_initializes_ticketpilot_only_when_enabled(monkeypatch) -> None:
    from service import service

    business_pool = object()

    @asynccontextmanager
    async def fake_initialize_database():
        yield object()

    @asynccontextmanager
    async def fake_initialize_store():
        yield object()

    @asynccontextmanager
    async def fake_get_ticketpilot_pool():
        yield business_pool

    apply_migrations = AsyncMock()
    ticketpilot_graph = object()
    build_ticketpilot_graph = Mock(return_value=ticketpilot_graph)
    load_agent = AsyncMock()
    get_agent = Mock()
    monkeypatch.setattr(service, "load_agent", load_agent)
    monkeypatch.setattr(service, "get_agent", get_agent)
    monkeypatch.setattr(service, "initialize_database", fake_initialize_database)
    monkeypatch.setattr(service, "initialize_store", fake_initialize_store)
    monkeypatch.setattr(service, "get_ticketpilot_pool", fake_get_ticketpilot_pool)
    monkeypatch.setattr(service, "apply_migrations", apply_migrations)
    monkeypatch.setattr(service, "build_ticketpilot_graph", build_ticketpilot_graph)
    monkeypatch.setattr(
        service, "get_all_agent_info", lambda: [AgentInfo(key="chatbot", description="")]
    )
    monkeypatch.setattr(service.settings, "DATABASE_TYPE", DatabaseType.POSTGRES)
    monkeypatch.setattr(service.settings, "TICKETPILOT_ENABLED", True)
    app = FastAPI()

    async with service.lifespan(app):
        assert app.state.ticketpilot_pool is business_pool
        assert app.state.ticketpilot_graph is ticketpilot_graph

    assert app.state.ticketpilot_pool is None
    assert app.state.ticketpilot_graph is None
    apply_migrations.assert_awaited_once_with(business_pool)
    build_ticketpilot_graph.assert_called_once()
    load_agent.assert_not_awaited()
    get_agent.assert_not_called()
