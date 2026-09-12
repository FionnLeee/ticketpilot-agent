import asyncio
import re
import sys
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver

from memory.postgres import get_postgres_saver
from ticketpilot.db import BusinessPool, apply_migrations, get_ticketpilot_pool
from ticketpilot.domain import (
    ApprovalDecision,
    PrincipalRole,
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
from ticketpilot.errors import Forbidden, ResourceNotFound, StateConflict
from ticketpilot.graph import build_ticketpilot_graph
from ticketpilot.orders import PostgresOrderRepository
from ticketpilot.policies import LocalPolicyRetriever
from ticketpilot.reasoning import DeterministicDemoReasoner
from ticketpilot.repositories import TicketRepository
from ticketpilot.schemas import (
    AddTicketMessageRequest,
    ApprovalDecisionRequest,
    Citation,
    CreateTicketRequest,
    RequestPrincipal,
    TicketClassification,
)
from ticketpilot.services import TicketService
from ticketpilot.workflow_repository import TicketWorkflowRepository

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.mark.docker
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,expected_status",
    [
        ("我不需要退款，只想知道物流", TicketStatus.RESOLVED),
        ("退款规则是什么？我只是咨询", TicketStatus.RESOLVED),
        ("我想退一部分", TicketStatus.WAITING_INFORMATION),
    ],
)
async def test_refund_counterexamples_never_create_approvals(message, expected_status):
    tenant = f"intent-{uuid4()}"
    principal = make_principal(tenant, PrincipalRole.CUSTOMER, "customer-a")
    reference = f"O-{uuid4()}"
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        order_id = await insert_order(pool, tenant, principal.actor_id, reference)
        try:
            service = make_service(pool, build_ticketpilot_graph(MemorySaver()))
            service.reasoner = DeterministicDemoReasoner()
            result = await service.create_ticket(
                principal,
                CreateTicketRequest(
                    subject="意图验收",
                    message=message,
                    order_reference=reference,
                ),
                f"create-{uuid4()}",
            )
            assert result.ticket.status is expected_status
            assert result.pending_approval is None
            if "只是咨询" in message:
                assert "政策依据" in result.latest_message.content
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT count(*) AS count FROM ticketpilot.approvals WHERE tenant_id = %s",
                    (tenant,),
                )
                assert (await cursor.fetchone())["count"] == 0
                cursor = await connection.execute(
                    "SELECT refundable_amount FROM ticketpilot.orders WHERE id = %s",
                    (order_id,),
                )
                assert (await cursor.fetchone())["refundable_amount"] == Decimal("399")
            if expected_status is TicketStatus.WAITING_INFORMATION:
                detail = await service.get_ticket(principal, result.ticket.id)
                assert detail.resolution_summary is None
                assert "明确" in result.latest_message.content
                request = AddTicketMessageRequest(message="我想退一部分")
                first = await service.add_message(principal, result.ticket.id, request, "clarify")
                before = await service.get_run_events(principal, first.run_id)
                repeated = await service.add_message(
                    principal, result.ticket.id, request, "clarify"
                )
                after = await service.get_run_events(principal, first.run_id)
                assert repeated.run_id == first.run_id
                assert repeated.ticket.status is TicketStatus.WAITING_INFORMATION
                assert before == after
        finally:
            await cleanup_tenant(pool, tenant)


@pytest.mark.docker
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial,linked,reply,amount",
    [
        ("我要退款100元", False, "order", "100"),
        ("我要全额退款", False, "order", "399"),
        ("我想退一部分", True, "100元", "100"),
    ],
)
async def test_pending_refund_survives_new_graph_and_only_fills_explicit_slots(
    initial, linked, reply, amount
):
    tenant = f"slots-{uuid4()}"
    principal = make_principal(tenant, PrincipalRole.CUSTOMER, "customer-a")
    reference = f"O-{uuid4()}"
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        await insert_order(pool, tenant, principal.actor_id, reference)
        try:
            service = make_service(pool, build_ticketpilot_graph(MemorySaver()))
            service.reasoner = DeterministicDemoReasoner()
            initial_result = await service.create_ticket(
                principal,
                CreateTicketRequest(
                    subject="补充信息",
                    message=initial,
                    order_reference=reference if linked else None,
                ),
                f"create-{uuid4()}",
            )
            assert initial_result.ticket.status is TicketStatus.WAITING_INFORMATION
            service = make_service(pool, build_ticketpilot_graph(MemorySaver()))
            service.reasoner = DeterministicDemoReasoner()
            result = await service.add_message(
                principal,
                initial_result.ticket.id,
                AddTicketMessageRequest(message=reference if reply == "order" else reply),
                "fill-slot",
            )
            assert result.ticket.status is TicketStatus.WAITING_APPROVAL
            assert Decimal(result.pending_approval.action_payload["amount"]) == Decimal(amount)
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT refundable_amount FROM ticketpilot.orders WHERE tenant_id = %s",
                    (tenant,),
                )
                assert (await cursor.fetchone())["refundable_amount"] == Decimal("399")
        finally:
            await cleanup_tenant(pool, tenant)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_cancelled_pending_refund_is_not_reused_and_order_conflict_skips_tools():
    tenant = f"cancel-{uuid4()}"
    principal = make_principal(tenant, PrincipalRole.CUSTOMER, "customer-a")
    reference = f"O-{uuid4()}"
    other_reference = f"O-{uuid4()}"
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        await insert_order(pool, tenant, principal.actor_id, reference)
        await insert_order(pool, tenant, principal.actor_id, other_reference)
        try:
            service = make_service(pool, build_ticketpilot_graph(MemorySaver()))
            service.reasoner = DeterministicDemoReasoner()
            created = await service.create_ticket(
                principal,
                CreateTicketRequest(
                    subject="取消后补订单",
                    message="我要退款100元",
                ),
                f"create-{uuid4()}",
            )
            await service.add_message(
                principal,
                created.ticket.id,
                AddTicketMessageRequest(message="算了，不退了"),
                "cancel",
            )
            supplied = await service.add_message(
                principal,
                created.ticket.id,
                AddTicketMessageRequest(message=reference),
                "supply-after-cancel",
            )
            assert supplied.pending_approval is None
            assert supplied.ticket.category is TicketCategory.ORDER_STATUS
            conflict = await service.add_message(
                principal,
                created.ticket.id,
                AddTicketMessageRequest(message=f"这次查询 {other_reference} 的物流"),
                "different-order",
            )
            assert conflict.ticket.status is TicketStatus.WAITING_INFORMATION
            assert conflict.ticket.order_reference == reference
            assert "新订单创建工单" in conflict.latest_message.content
            events = await service.get_run_events(principal, conflict.run_id)
            assert not any(event.tool_name for event in events.events)
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT count(*) AS count FROM ticketpilot.approvals WHERE tenant_id = %s",
                    (tenant,),
                )
                assert (await cursor.fetchone())["count"] == 0
        finally:
            await cleanup_tenant(pool, tenant)


class FakeReasoner:
    async def classify(
        self,
        message: str,
        known_order_reference: str | None,
        config: RunnableConfig,
    ) -> TicketClassification:
        extracted = re.search(r"O-[0-9a-f-]{36}", message)
        order_reference = known_order_reference or (extracted.group(0) if extracted else None)
        if "退款" in message:
            return TicketClassification(
                category=TicketCategory.REFUND,
                priority=TicketPriority.HIGH,
                order_reference=order_reference,
                requested_refund_amount=Decimal("100.00"),
            )
        return TicketClassification(
            category=TicketCategory.ORDER_STATUS,
            order_reference=order_reference,
        )

    async def answer(
        self,
        message: str,
        classification: TicketClassification,
        order_result: dict | None,
        policy_evidence: list[Citation],
        config: RunnableConfig,
    ) -> str:
        assert order_result is not None
        assert order_result["found"] is True
        assert policy_evidence
        return (
            f"订单当前为 {order_result['order']['fulfillment_status']}，"
            f"依据 {policy_evidence[0].chunk_id}。"
        )


class FailingReasoner(FakeReasoner):
    async def classify(
        self,
        message: str,
        known_order_reference: str | None,
        config: RunnableConfig,
    ) -> TicketClassification:
        raise RuntimeError("model unavailable")


class TimeoutOrderReader:
    async def get_by_reference(self, principal: RequestPrincipal, order_reference: str):
        raise TimeoutError


def make_principal(tenant_id: str, role: PrincipalRole, actor_id: str) -> RequestPrincipal:
    return RequestPrincipal(tenant_id=tenant_id, actor_id=actor_id, role=role)


def make_service(pool: BusinessPool, graph) -> TicketService:
    return TicketService(
        TicketRepository(pool),
        workflow=graph,
        workflow_repository=TicketWorkflowRepository(pool),
        order_reader=PostgresOrderRepository(pool),
        policy_retriever=LocalPolicyRetriever(),
        reasoner=FakeReasoner(),
    )


async def insert_order(
    pool: BusinessPool,
    tenant_id: str,
    customer_id: str,
    order_reference: str,
) -> UUID:
    order_id = uuid4()
    async with pool.connection() as connection:
        await connection.execute(
            """
            INSERT INTO ticketpilot.orders (
                id, tenant_id, order_reference, customer_id,
                payment_status, fulfillment_status, paid_amount,
                refundable_amount, currency, carrier, tracking_number,
                estimated_delivery_at
            ) VALUES (%s, %s, %s, %s, 'PAID', 'SHIPPED', 399.00,
                      399.00, 'CNY', 'SF Express', 'TR1234567890', now() + interval '2 days')
            """,
            (order_id, tenant_id, order_reference, customer_id),
        )
    return order_id


async def cleanup_tenant(pool: BusinessPool, tenant_id: str) -> None:
    async with pool.connection() as connection, connection.transaction():
        await connection.execute(
            "DELETE FROM ticketpilot.audit_events WHERE tenant_id = %s", (tenant_id,)
        )
        await connection.execute(
            "DELETE FROM ticketpilot.ticket_messages WHERE tenant_id = %s", (tenant_id,)
        )
        await connection.execute(
            "DELETE FROM ticketpilot.approvals WHERE tenant_id = %s", (tenant_id,)
        )
        await connection.execute(
            "DELETE FROM ticketpilot.tickets WHERE tenant_id = %s", (tenant_id,)
        )
        await connection.execute(
            "DELETE FROM ticketpilot.orders WHERE tenant_id = %s", (tenant_id,)
        )


@pytest.mark.docker
@pytest.mark.asyncio
async def test_day11_normal_order_flow_is_grounded_and_resolved() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = make_principal(tenant_id, PrincipalRole.CUSTOMER, "customer-a")
    order_reference = f"O-{uuid4()}"
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        await insert_order(pool, tenant_id, customer.actor_id, order_reference)
        try:
            service = make_service(pool, build_ticketpilot_graph(MemorySaver()))
            result = await service.create_ticket(
                customer,
                CreateTicketRequest(
                    subject="查询物流",
                    message="我的物流什么时候送达？",
                    order_reference=order_reference,
                ),
                f"create-{uuid4()}",
            )
            detail = await service.get_ticket(customer, result.ticket.id)
            events = await service.get_run_events(customer, result.run_id)

            assert result.ticket.status is TicketStatus.RESOLVED
            assert result.ticket.category is TicketCategory.ORDER_STATUS
            assert detail.order is not None
            assert detail.order.tracking_number_masked == "TR******7890"
            assert detail.messages[-1].role.value == "AGENT"
            assert detail.messages[-1].citations[0].chunk_id == "order-tracking"
            assert [event.event_type for event in events.events] == [
                "TICKET_CREATED",
                "RUN_STARTED",
                "TICKET_TRIAGED",
                "TOOL_SUCCEEDED",
                "TOOL_SUCCEEDED",
                "TICKET_RESOLVED",
            ]
            tool_events = [event for event in events.events if event.tool_name]
            assert [event.tool_name for event in tool_events] == ["query_order", "search_policy"]
            assert all(isinstance(event.details["duration_ms"], int) for event in tool_events)
        finally:
            await cleanup_tenant(pool, tenant_id)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_day14_add_message_reuses_thread_and_is_idempotent() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = make_principal(tenant_id, PrincipalRole.CUSTOMER, "customer-a")
    order_reference = f"O-{uuid4()}"
    saver = MemorySaver()
    thread_id = ""
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        await insert_order(pool, tenant_id, customer.actor_id, order_reference)
        try:
            service = make_service(pool, build_ticketpilot_graph(saver))
            initial = await service.create_ticket(
                customer,
                CreateTicketRequest(
                    subject="查询物流",
                    message="我的物流什么时候送达？",
                    order_reference=order_reference,
                ),
                f"create-{uuid4()}",
            )
            thread_id = initial.ticket.thread_id
            follow_up = await service.add_message(
                customer,
                initial.ticket.id,
                AddTicketMessageRequest(message="请再次确认物流状态"),
                "follow-up-1",
            )
            replay = await service.add_message(
                customer,
                initial.ticket.id,
                AddTicketMessageRequest(message="请再次确认物流状态"),
                "follow-up-1",
            )
            detail = await service.get_ticket(customer, initial.ticket.id)
            events = await service.get_run_events(customer, follow_up.run_id)

            assert follow_up.ticket.status is TicketStatus.RESOLVED
            assert follow_up.ticket.thread_id == initial.ticket.thread_id
            assert follow_up.run_id != initial.run_id
            assert replay.run_id == follow_up.run_id
            assert [message.role.value for message in detail.messages] == [
                "CUSTOMER",
                "AGENT",
                "CUSTOMER",
                "AGENT",
            ]
            assert [event.event_type for event in events.events] == [
                "MESSAGE_ADDED",
                "RUN_STARTED",
                "TICKET_TRIAGED",
                "TOOL_SUCCEEDED",
                "TOOL_SUCCEEDED",
                "TICKET_RESOLVED",
            ]
            with pytest.raises(StateConflict):
                await service.add_message(
                    customer,
                    initial.ticket.id,
                    AddTicketMessageRequest(message="同一个键但正文不同"),
                    "follow-up-1",
                )
            persisted_only = await service.repository.append_message(
                customer,
                initial.ticket.id,
                AddTicketMessageRequest(message="模拟物流查询提交后、调度前进程退出"),
                "follow-up-recovery",
            )
            recovered = await service.add_message(
                customer,
                initial.ticket.id,
                AddTicketMessageRequest(message="模拟物流查询提交后、调度前进程退出"),
                "follow-up-recovery",
            )
            recovered_events = await service.get_run_events(customer, recovered.run_id)

            assert recovered.run_id == persisted_only.run_id
            assert recovered.ticket.status is TicketStatus.RESOLVED
            assert "RUN_STARTED" in [event.event_type for event in recovered_events.events]
            staff = make_principal(tenant_id, PrincipalRole.STAFF, "staff-a")
            staff_result = await service.add_message(
                staff,
                initial.ticket.id,
                AddTicketMessageRequest(message="请以客服身份再次查询物流"),
                "staff-follow-up",
            )
            staff_detail = await service.get_ticket(customer, initial.ticket.id)

            assert staff_result.ticket.thread_id == initial.ticket.thread_id
            assert staff_detail.messages[-2].role.value == "STAFF"
            assert staff_detail.messages[-2].content == "请以客服身份再次查询物流"
        finally:
            if thread_id:
                await saver.adelete_thread(thread_id)
            await cleanup_tenant(pool, tenant_id)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_day14_order_timeout_is_audited_and_fails_closed() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = make_principal(tenant_id, PrincipalRole.CUSTOMER, "customer-a")
    order_reference = f"O-{uuid4()}"
    saver = MemorySaver()
    thread_id = ""
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        await insert_order(pool, tenant_id, customer.actor_id, order_reference)
        try:
            service = TicketService(
                TicketRepository(pool),
                workflow=build_ticketpilot_graph(saver),
                workflow_repository=TicketWorkflowRepository(pool),
                order_reader=TimeoutOrderReader(),
                policy_retriever=LocalPolicyRetriever(),
                reasoner=FakeReasoner(),
            )
            result = await service.create_ticket(
                customer,
                CreateTicketRequest(
                    subject="依赖超时",
                    message="查询订单物流",
                    order_reference=order_reference,
                ),
                f"create-{uuid4()}",
            )
            thread_id = result.ticket.thread_id
            events = await service.get_run_events(customer, result.run_id)
            order_event = next(event for event in events.events if event.tool_name == "query_order")

            assert result.ticket.status is TicketStatus.RESOLVED
            assert "未找到" in result.latest_message.content
            assert order_event.event_type == "TOOL_FAILED"
            assert order_event.details["error_code"] == "DEPENDENCY_TIMEOUT"
        finally:
            if thread_id:
                await saver.adelete_thread(thread_id)
            await cleanup_tenant(pool, tenant_id)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_day14_unhandled_reasoner_failure_marks_ticket_failed() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = make_principal(tenant_id, PrincipalRole.CUSTOMER, "customer-a")
    order_reference = f"O-{uuid4()}"
    saver = MemorySaver()
    thread_id = ""
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        await insert_order(pool, tenant_id, customer.actor_id, order_reference)
        try:
            service = TicketService(
                TicketRepository(pool),
                workflow=build_ticketpilot_graph(saver),
                workflow_repository=TicketWorkflowRepository(pool),
                order_reader=PostgresOrderRepository(pool),
                policy_retriever=LocalPolicyRetriever(),
                reasoner=FailingReasoner(),
            )
            with pytest.raises(RuntimeError, match="model unavailable"):
                await service.create_ticket(
                    customer,
                    CreateTicketRequest(
                        subject="模型失败",
                        message="查询订单物流",
                        order_reference=order_reference,
                    ),
                    f"create-{uuid4()}",
                )
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT id, thread_id FROM ticketpilot.tickets
                    WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1
                    """,
                    (tenant_id,),
                )
                ticket = await cursor.fetchone()
                cursor = await connection.execute(
                    """
                    SELECT run_id FROM ticketpilot.audit_events
                    WHERE tenant_id = %s AND ticket_id = %s AND event_type = 'RUN_FAILED'
                    """,
                    (tenant_id, ticket["id"]),
                )
                failure = await cursor.fetchone()
            thread_id = ticket["thread_id"]
            detail = await service.get_ticket(customer, ticket["id"])
            events = await service.get_run_events(customer, failure["run_id"])

            assert detail.status is TicketStatus.FAILED
            assert events.events[-1].event_type == "RUN_FAILED"
            assert events.events[-1].details["error_code"] == "RuntimeError"
        finally:
            if thread_id:
                await saver.adelete_thread(thread_id)
            await cleanup_tenant(pool, tenant_id)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_day11_model_extracted_order_is_authorized_before_linking() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = make_principal(tenant_id, PrincipalRole.CUSTOMER, "customer-a")
    order_reference = f"O-{uuid4()}"
    saver = MemorySaver()
    thread_id = ""
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        order_id = await insert_order(pool, tenant_id, customer.actor_id, order_reference)
        try:
            service = make_service(pool, build_ticketpilot_graph(saver))
            result = await service.create_ticket(
                customer,
                CreateTicketRequest(
                    subject="查询物流",
                    message=f"请查询订单 {order_reference} 的物流",
                ),
                f"create-{uuid4()}",
            )
            thread_id = result.ticket.thread_id
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT order_pk FROM ticketpilot.tickets
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, result.ticket.id),
                )
                ticket = await cursor.fetchone()

            assert result.ticket.status is TicketStatus.RESOLVED
            assert result.ticket.order_reference == order_reference
            assert ticket == {"order_pk": order_id}
        finally:
            if thread_id:
                await saver.adelete_thread(thread_id)
            await cleanup_tenant(pool, tenant_id)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_day12_postgres_checkpoint_resume_and_refund_idempotency() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = make_principal(tenant_id, PrincipalRole.CUSTOMER, "customer-a")
    approver = make_principal(tenant_id, PrincipalRole.APPROVER, "approver-a")
    other_approver = make_principal(f"tenant-{uuid4()}", PrincipalRole.APPROVER, "approver-b")
    order_reference = f"O-{uuid4()}"
    thread_id = ""
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        order_id = await insert_order(pool, tenant_id, customer.actor_id, order_reference)
        try:
            async with get_postgres_saver() as first_saver:
                service = make_service(pool, build_ticketpilot_graph(first_saver))
                pending = await service.create_ticket(
                    customer,
                    CreateTicketRequest(
                        subject="申请退款",
                        message="请给这个订单退款 100 元",
                        order_reference=order_reference,
                    ),
                    f"create-{uuid4()}",
                )
                thread_id = pending.ticket.thread_id
                assert pending.ticket.status is TicketStatus.WAITING_APPROVAL
                assert pending.ticket.risk_level.value == "HIGH_RISK_WRITE"
                assert pending.pending_approval is not None
                approval_id = pending.pending_approval.id

                with pytest.raises(StateConflict):
                    await service.add_message(
                        customer,
                        pending.ticket.id,
                        AddTicketMessageRequest(message="用普通消息批准退款"),
                        "message-cannot-approve",
                    )

                async with pool.connection() as connection:
                    cursor = await connection.execute(
                        """
                        SELECT refundable_amount FROM ticketpilot.orders
                        WHERE tenant_id = %s AND id = %s
                        """,
                        (tenant_id, order_id),
                    )
                    before_approval = await cursor.fetchone()
                    cursor = await connection.execute(
                        """
                        SELECT count(*) AS count FROM ticketpilot.audit_events
                        WHERE tenant_id = %s AND ticket_id = %s
                          AND event_type = 'REFUND_EXECUTED'
                        """,
                        (tenant_id, pending.ticket.id),
                    )
                    before_execution_count = await cursor.fetchone()

                assert before_approval == {"refundable_amount": Decimal("399.00")}
                assert before_execution_count == {"count": 0}

                with pytest.raises(Forbidden):
                    await service.decide_approval(
                        customer,
                        approval_id,
                        ApprovalDecisionRequest(
                            decision=ApprovalDecision.APPROVE,
                            reason="customer cannot approve",
                        ),
                    )

                with pytest.raises(ResourceNotFound):
                    await service.decide_approval(
                        other_approver,
                        approval_id,
                        ApprovalDecisionRequest(
                            decision=ApprovalDecision.APPROVE,
                            reason="wrong tenant",
                        ),
                    )

            async with get_postgres_saver() as second_saver:
                restarted_service = make_service(pool, build_ticketpilot_graph(second_saver))
                approved = await restarted_service.decide_approval(
                    approver,
                    approval_id,
                    ApprovalDecisionRequest(
                        decision=ApprovalDecision.APPROVE,
                        reason="测试批准",
                    ),
                )
                repeated = await restarted_service.decide_approval(
                    approver,
                    approval_id,
                    ApprovalDecisionRequest(
                        decision=ApprovalDecision.APPROVE,
                        reason="重复批准",
                    ),
                )
                initial_events = await restarted_service.get_run_events(customer, pending.run_id)
                approval_events = await restarted_service.get_run_events(approver, approved.run_id)
                replay_events = await restarted_service.get_run_events(approver, repeated.run_id)
                with pytest.raises(StateConflict):
                    await restarted_service.decide_approval(
                        approver,
                        approval_id,
                        ApprovalDecisionRequest(
                            decision=ApprovalDecision.REJECT,
                            reason="conflicting decision",
                        ),
                    )
                await second_saver.adelete_thread(thread_id)

            async with pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT payment_status, refundable_amount
                    FROM ticketpilot.orders
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, order_id),
                )
                order = await cursor.fetchone()
                cursor = await connection.execute(
                    """
                    SELECT status FROM ticketpilot.approvals
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, approval_id),
                )
                approval = await cursor.fetchone()
                cursor = await connection.execute(
                    """
                    SELECT count(*) AS count FROM ticketpilot.audit_events
                    WHERE tenant_id = %s AND ticket_id = %s
                      AND event_type = 'REFUND_EXECUTED'
                    """,
                    (tenant_id, approved.ticket.id),
                )
                execution_count = await cursor.fetchone()

            assert approved.ticket.status is TicketStatus.RESOLVED
            assert repeated.ticket.status is TicketStatus.RESOLVED
            assert order == {
                "payment_status": "PARTIALLY_REFUNDED",
                "refundable_amount": Decimal("299.00"),
            }
            assert approval == {"status": "EXECUTED"}
            assert execution_count == {"count": 1}
            assert initial_events.events[-1].event_type == "REFUND_APPROVAL_REQUESTED"
            assert [event.event_type for event in approval_events.events] == [
                "APPROVAL_DECIDED",
                "REFUND_EXECUTED",
            ]
            assert [event.event_type for event in replay_events.events] == [
                "APPROVAL_DECISION_REPLAYED"
            ]
        finally:
            if thread_id:
                async with get_postgres_saver() as saver:
                    await saver.adelete_thread(thread_id)
            await cleanup_tenant(pool, tenant_id)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_refund_action_id_distinguishes_retry_from_same_amount_new_intent() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = make_principal(tenant_id, PrincipalRole.CUSTOMER, "customer-a")
    approver = make_principal(tenant_id, PrincipalRole.APPROVER, "approver-a")
    order_reference = f"O-{uuid4()}"
    saver = MemorySaver()
    thread_id = ""

    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        order_id = await insert_order(pool, tenant_id, customer.actor_id, order_reference)
        try:
            service = make_service(pool, build_ticketpilot_graph(saver))
            request = CreateTicketRequest(
                subject="两次退款",
                message="我要退款 100 元",
                order_reference=order_reference,
            )
            first = await service.create_ticket(customer, request, "create-refund-attempt")
            retried_create = await service.create_ticket(customer, request, "create-refund-attempt")
            thread_id = first.ticket.thread_id

            assert first.ticket.id == retried_create.ticket.id
            assert first.run_id == retried_create.run_id
            assert first.pending_approval is not None
            assert retried_create.pending_approval is not None
            assert first.pending_approval.id == retried_create.pending_approval.id

            async with pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT action_id
                    FROM ticketpilot.approvals
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, first.pending_approval.id),
                )
                first_action_id = (await cursor.fetchone())["action_id"]
                await connection.execute(
                    """
                    UPDATE ticketpilot.tickets
                    SET active_run_id = %s, run_started = true
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (first.run_id, tenant_id, first.ticket.id),
                )
            assert service.workflow_repository is not None
            replayed_action = await service.workflow_repository.create_pending_refund(
                customer,
                first.ticket.id,
                first.run_id,
                first_action_id,
                order_reference,
                Decimal("100"),
            )
            await service.repository.release_run(tenant_id, first.ticket.id, first.run_id)
            assert replayed_action.id == first.pending_approval.id

            first_approved = await service.decide_approval(
                approver,
                first.pending_approval.id,
                ApprovalDecisionRequest(
                    decision=ApprovalDecision.APPROVE,
                    reason="第一次申请通过",
                ),
            )
            assert first_approved.ticket.status is TicketStatus.RESOLVED

            second = await service.add_message(
                customer,
                first.ticket.id,
                AddTicketMessageRequest(message="我要退款 100 元"),
                "second-refund-attempt",
            )
            retried_second = await service.add_message(
                customer,
                first.ticket.id,
                AddTicketMessageRequest(message="我要退款 100 元"),
                "second-refund-attempt",
            )

            assert second.run_id == retried_second.run_id
            assert second.pending_approval is not None
            assert retried_second.pending_approval is not None
            assert second.pending_approval.id == retried_second.pending_approval.id
            assert second.pending_approval.id != first.pending_approval.id

            second_approved = await service.decide_approval(
                approver,
                second.pending_approval.id,
                ApprovalDecisionRequest(
                    decision=ApprovalDecision.APPROVE,
                    reason="第二次独立申请通过",
                ),
            )
            assert second_approved.ticket.status is TicketStatus.RESOLVED

            async with pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT payment_status, refundable_amount
                    FROM ticketpilot.orders
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, order_id),
                )
                order = await cursor.fetchone()
                cursor = await connection.execute(
                    """
                    SELECT id, action_id, action_payload, status
                    FROM ticketpilot.approvals
                    WHERE tenant_id = %s AND ticket_id = %s
                    ORDER BY requested_at, id
                    """,
                    (tenant_id, first.ticket.id),
                )
                approvals = list(await cursor.fetchall())
                cursor = await connection.execute(
                    """
                    SELECT
                        count(*) FILTER (
                            WHERE event_type = 'REFUND_EXECUTED'
                        ) AS executions,
                        count(*) FILTER (
                            WHERE event_type = 'REFUND_APPROVAL_REPLAYED'
                        ) AS action_replays
                    FROM ticketpilot.audit_events
                    WHERE tenant_id = %s AND ticket_id = %s
                    """,
                    (tenant_id, first.ticket.id),
                )
                event_counts = await cursor.fetchone()

            assert order == {
                "payment_status": "PARTIALLY_REFUNDED",
                "refundable_amount": Decimal("199.00"),
            }
            assert len(approvals) == 2
            assert len({approval["action_id"] for approval in approvals}) == 2
            assert all(approval["status"] == "EXECUTED" for approval in approvals)
            assert all(
                Decimal(approval["action_payload"]["amount"]) == Decimal("100")
                for approval in approvals
            )
            assert event_counts == {"executions": 2, "action_replays": 1}
        finally:
            if thread_id:
                await saver.adelete_thread(thread_id)
            await cleanup_tenant(pool, tenant_id)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_day12_revalidates_order_after_approval() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = make_principal(tenant_id, PrincipalRole.CUSTOMER, "customer-a")
    approver = make_principal(tenant_id, PrincipalRole.APPROVER, "approver-a")
    order_reference = f"O-{uuid4()}"
    saver = MemorySaver()
    thread_id = ""
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        order_id = await insert_order(pool, tenant_id, customer.actor_id, order_reference)
        try:
            service = make_service(pool, build_ticketpilot_graph(saver))
            pending = await service.create_ticket(
                customer,
                CreateTicketRequest(
                    subject="申请退款",
                    message="我要退款 100 元",
                    order_reference=order_reference,
                ),
                f"create-{uuid4()}",
            )
            thread_id = pending.ticket.thread_id
            assert pending.pending_approval is not None
            approval_id = pending.pending_approval.id

            async with pool.connection() as connection:
                await connection.execute(
                    """
                    UPDATE ticketpilot.orders
                    SET payment_status = 'REFUNDED', refundable_amount = 0,
                        version = version + 1
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, order_id),
                )

            blocked = await service.decide_approval(
                approver,
                approval_id,
                ApprovalDecisionRequest(
                    decision=ApprovalDecision.APPROVE,
                    reason="批准但订单随后变化",
                ),
            )
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT status FROM ticketpilot.approvals
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, approval_id),
                )
                approval = await cursor.fetchone()

            assert blocked.ticket.status is TicketStatus.FAILED
            assert approval == {"status": "CANCELLED"}
            assert "安全阻断" in blocked.latest_message.content
        finally:
            if thread_id:
                await saver.adelete_thread(thread_id)
            await cleanup_tenant(pool, tenant_id)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_day12_rejection_never_changes_order_amount() -> None:
    tenant_id = f"tenant-{uuid4()}"
    customer = make_principal(tenant_id, PrincipalRole.CUSTOMER, "customer-a")
    approver = make_principal(tenant_id, PrincipalRole.APPROVER, "approver-a")
    order_reference = f"O-{uuid4()}"
    saver = MemorySaver()
    thread_id = ""
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        order_id = await insert_order(pool, tenant_id, customer.actor_id, order_reference)
        try:
            service = make_service(pool, build_ticketpilot_graph(saver))
            pending = await service.create_ticket(
                customer,
                CreateTicketRequest(
                    subject="申请退款",
                    message="我要退款 100 元",
                    order_reference=order_reference,
                ),
                f"create-{uuid4()}",
            )
            thread_id = pending.ticket.thread_id
            assert pending.pending_approval is not None

            rejected = await service.decide_approval(
                approver,
                pending.pending_approval.id,
                ApprovalDecisionRequest(
                    decision=ApprovalDecision.REJECT,
                    reason="不符合条件",
                ),
            )
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT refundable_amount FROM ticketpilot.orders
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, order_id),
                )
                order = await cursor.fetchone()

            assert rejected.ticket.status is TicketStatus.RESOLVED
            assert order == {"refundable_amount": Decimal("399.00")}
            assert "未执行任何退款" in rejected.latest_message.content
        finally:
            if thread_id:
                await saver.adelete_thread(thread_id)
            await cleanup_tenant(pool, tenant_id)
