import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from langgraph.checkpoint.memory import MemorySaver
from test_workflow_graph import cleanup_tenant, insert_order, make_principal, make_service

from core import settings
from ticketpilot.api import get_ticket_service, install_ticketpilot_api
from ticketpilot.db import apply_migrations, get_ticketpilot_pool
from ticketpilot.domain import (
    ApprovalDecision,
    PrincipalRole,
    RiskLevel,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from ticketpilot.errors import StateConflict
from ticketpilot.graph import build_ticketpilot_graph
from ticketpilot.schemas import (
    AddTicketMessageRequest,
    ApprovalDecisionRequest,
    CreateTicketRequest,
)


class PausedGraph:
    def __init__(self, graph, *, after=False):
        self.graph = graph
        self.after = after
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def ainvoke(self, *args, **kwargs):
        if self.after:
            result = await self.graph.ainvoke(*args, **kwargs)
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), 10)
        if not self.after:
            result = await self.graph.ainvoke(*args, **kwargs)
        return result


@pytest.mark.docker
@pytest.mark.asyncio
async def test_create_reserves_run_and_cooperative_cancellation_releases_it(monkeypatch):
    tenant = f"create-owner-{uuid4()}"
    principal = make_principal(tenant, PrincipalRole.CUSTOMER, "customer-a")
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        reference = f"O-{uuid4()}"
        await insert_order(pool, tenant, principal.actor_id, reference)
        graph = build_ticketpilot_graph(MemorySaver())
        service = make_service(pool, graph)
        paused = PausedGraph(graph)
        task = asyncio.create_task(
            make_service(pool, paused).create_ticket(
                principal,
                CreateTicketRequest(
                    subject="创建占用", message="查询物流", order_reference=reference
                ),
                "create-reservation",
            )
        )
        try:
            await asyncio.wait_for(paused.entered.wait(), 10)
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT id FROM ticketpilot.tickets WHERE tenant_id = %s", (tenant,)
                )
                ticket_id = (await cursor.fetchone())["id"]
            with pytest.raises(StateConflict):
                await service.add_message(
                    principal,
                    ticket_id,
                    AddTicketMessageRequest(message="创建仍在执行时追加"),
                    "busy-create",
                )
            monkeypatch.setattr(
                settings,
                "TICKETPILOT_AUTH_TOKENS",
                {
                    "test-owner": {
                        "tenant_id": tenant,
                        "actor_id": principal.actor_id,
                        "role": "CUSTOMER",
                    }
                },
            )
            app = FastAPI()
            install_ticketpilot_api(app)
            app.dependency_overrides[get_ticket_service] = lambda: service
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/v1/tickets/{ticket_id}/messages",
                    headers={"Authorization": "Bearer test-owner", "Idempotency-Key": "http-busy"},
                    json={"message": "HTTP 并发消息"},
                )
                assert response.status_code == 409
            detail = await service.get_ticket(principal, ticket_id)
            assert [message.content for message in detail.messages] == ["查询物流"]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            ticket = await service.repository.get_ticket(principal, ticket_id)
            assert ticket["status"] == TicketStatus.FAILED.value
            assert ticket["active_run_id"] is None
            result = await service.add_message(
                principal,
                ticket_id,
                AddTicketMessageRequest(message="取消之后重新查询物流"),
                "after-cancel",
            )
            assert result.ticket.status is TicketStatus.RESOLVED
        finally:
            paused.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await cleanup_tenant(pool, tenant)


@pytest.mark.docker
@pytest.mark.asyncio
@pytest.mark.parametrize("after", [False, True])
async def test_message_is_busy_before_graph_start_and_until_graph_returns(after):
    tenant = f"ownership-{uuid4()}"
    principal = make_principal(tenant, PrincipalRole.CUSTOMER, "customer-a")
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        reference = f"O-{uuid4()}"
        await insert_order(pool, tenant, principal.actor_id, reference)
        graph = build_ticketpilot_graph(MemorySaver())
        service = make_service(pool, graph)
        paused = PausedGraph(graph, after=after)
        task = None
        try:
            created = await service.create_ticket(
                principal,
                CreateTicketRequest(subject="并发", message="查询物流", order_reference=reference),
                "create-concurrency",
            )
            service_a = make_service(pool, paused)
            task = asyncio.create_task(
                service_a.add_message(
                    principal,
                    created.ticket.id,
                    AddTicketMessageRequest(message="消息 A 查询物流"),
                    "a",
                )
            )
            await asyncio.wait_for(paused.entered.wait(), 10)
            with pytest.raises(StateConflict):
                await service.add_message(
                    principal,
                    created.ticket.id,
                    AddTicketMessageRequest(message="消息 B 查询物流"),
                    "b",
                )
            with pytest.raises(StateConflict):
                await service.add_message(
                    principal,
                    created.ticket.id,
                    AddTicketMessageRequest(message="消息 A 查询物流"),
                    "a",
                )
            paused.release.set()
            result = await task
            detail = await service.get_ticket(principal, created.ticket.id)
            assert result.ticket.status is TicketStatus.RESOLVED
            assert "消息 A 查询物流" in [message.content for message in detail.messages]
            assert "消息 B 查询物流" not in [message.content for message in detail.messages]
        finally:
            paused.release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await cleanup_tenant(pool, tenant)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_run_reads_its_bound_message_and_rejects_stale_writers():
    tenant = f"bound-{uuid4()}"
    principal = make_principal(tenant, PrincipalRole.CUSTOMER, "customer-a")
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        reference = f"O-{uuid4()}"
        await insert_order(pool, tenant, principal.actor_id, reference)
        service = make_service(pool, build_ticketpilot_graph(MemorySaver()))
        run_id = None
        try:
            created = await service.create_ticket(
                principal,
                CreateTicketRequest(subject="绑定", message="查询物流", order_reference=reference),
                "create-binding",
            )
            queued = await service.repository.append_message(
                principal,
                created.ticket.id,
                AddTicketMessageRequest(message="绑定消息 A"),
                "queued",
            )
            run_id = queued.run_id
            claims = await asyncio.gather(
                service.repository.claim_run(tenant, created.ticket.id, run_id),
                service.repository.claim_run(tenant, created.ticket.id, run_id),
                return_exceptions=True,
            )
            assert sum(result is None for result in claims) == 1
            assert sum(isinstance(result, StateConflict) for result in claims) == 1
            async with pool.connection() as connection:
                await connection.execute(
                    """INSERT INTO ticketpilot.ticket_messages
                    (id, tenant_id, ticket_id, run_id, role, content, citations)
                    VALUES (%s, %s, %s, %s, 'CUSTOMER', '人为插入的更新消息 B', '[]')""",
                    (uuid4(), tenant, created.ticket.id, uuid4()),
                )
            workflow = service.workflow_repository
            context = await workflow.begin_run(principal, created.ticket.id, run_id)
            assert context.customer_message == "绑定消息 A"
            before = await service.repository.get_ticket(principal, created.ticket.id)
            events_before = await service.get_run_events(principal, created.run_id)
            await workflow.fail_run(tenant, created.ticket.id, created.run_id, "late_failure")
            await service.repository.release_run(tenant, created.ticket.id, created.run_id)
            with pytest.raises(StateConflict):
                await workflow.save_triage(
                    tenant,
                    created.ticket.id,
                    created.run_id,
                    TicketCategory.REFUND,
                    TicketPriority.HIGH,
                    RiskLevel.HIGH_RISK_WRITE,
                )
            with pytest.raises(StateConflict):
                await workflow.resolve_ticket(
                    tenant, created.ticket.id, created.run_id, "stale", []
                )
            with pytest.raises(StateConflict):
                await workflow.request_information(
                    tenant, created.ticket.id, created.run_id, "stale", {}
                )
            assert await service.repository.get_ticket(principal, created.ticket.id) == before
            assert await service.get_run_events(principal, created.run_id) == events_before
            await workflow.resolve_ticket(
                tenant, created.ticket.id, run_id, "当前 run 正常完成", []
            )
        finally:
            if run_id is not None:
                await service.repository.release_run(tenant, created.ticket.id, run_id)
            await cleanup_tenant(pool, tenant)


@pytest.mark.docker
@pytest.mark.asyncio
@pytest.mark.parametrize("after", [False, True])
async def test_concurrent_approval_cannot_start_a_second_resume(after):
    tenant = f"approval-owner-{uuid4()}"
    principal = make_principal(tenant, PrincipalRole.CUSTOMER, "customer-a")
    approver = make_principal(tenant, PrincipalRole.APPROVER, "approver-a")
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        reference = f"O-{uuid4()}"
        await insert_order(pool, tenant, principal.actor_id, reference)
        graph = build_ticketpilot_graph(MemorySaver())
        service = make_service(pool, graph)
        paused = PausedGraph(graph, after=after)
        task = None
        try:
            created = await service.create_ticket(
                principal,
                CreateTicketRequest(
                    subject="退款", message="申请退款100元", order_reference=reference
                ),
                "create-late-failure",
            )
            decision = ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE, reason="并发验收")
            approval_id = created.pending_approval.id
            task = asyncio.create_task(
                make_service(pool, paused).decide_approval(approver, approval_id, decision)
            )
            await asyncio.wait_for(paused.entered.wait(), 10)
            with pytest.raises(StateConflict):
                await service.decide_approval(approver, approval_id, decision)
            paused.release.set()
            completed = await task
            assert completed.ticket.status is TicketStatus.RESOLVED
            await service.decide_approval(approver, approval_id, decision)
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT refundable_amount FROM ticketpilot.orders WHERE tenant_id = %s",
                    (tenant,),
                )
                assert str((await cursor.fetchone())["refundable_amount"]) == "299.00"
                cursor = await connection.execute(
                    "SELECT count(*) AS count FROM ticketpilot.audit_events WHERE tenant_id = %s AND event_type = 'REFUND_EXECUTED'",
                    (tenant,),
                )
                assert (await cursor.fetchone())["count"] == 1
        finally:
            paused.release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await cleanup_tenant(pool, tenant)
