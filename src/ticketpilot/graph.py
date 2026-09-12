import json
import re
from dataclasses import dataclass
from decimal import Decimal
from time import time
from typing import Any, Literal, cast
from uuid import UUID

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from ticketpilot.domain import (
    ApprovalStatus,
    RiskLevel,
    TicketCategory,
)
from ticketpilot.reasoning import ORDER_REFERENCE_PATTERN, TicketReasoner
from ticketpilot.schemas import Citation, TicketClassification
from ticketpilot.tools import TicketPilotContext, query_order, search_policy
from ticketpilot.workflow_repository import TicketWorkflowRepository


class TicketAgentState(MessagesState, total=False):
    ticket_id: str
    run_id: str
    action_id: str
    customer_message: str
    order_reference: str | None
    classification: dict[str, Any]
    risk_level: str
    order_result: dict[str, Any]
    order_tool_started_at: float
    policy_result: dict[str, Any]
    policy_tool_started_at: float
    proposed_refund_amount: str
    approval_id: str
    resume_payload: Any
    final_answer: str
    pending_request: dict[str, Any]
    clarification: str
    order_conflict: bool


@dataclass(frozen=True, kw_only=True)
class TicketGraphContext(TicketPilotContext):
    workflow_repository: TicketWorkflowRepository
    reasoner: TicketReasoner


def _required_uuid(
    state: TicketAgentState, key: Literal["ticket_id", "run_id", "action_id"]
) -> UUID:
    value = cast(str | None, state.get(key))
    if not value:
        raise ValueError(f"Ticket graph requires {key}")
    return UUID(value)


def _tool_payload(message: ToolMessage) -> dict[str, Any]:
    content = message.content
    if isinstance(content, str):
        payload = json.loads(content)
    elif isinstance(content, dict):
        payload = content
    else:
        raise TypeError("TicketPilot ToolMessage must contain an object")
    if not isinstance(payload, dict):
        raise TypeError("TicketPilot ToolMessage payload must be an object")
    return payload


async def load_ticket_context(
    state: TicketAgentState, runtime: Runtime[TicketGraphContext]
) -> dict[str, Any]:
    ticket_id = _required_uuid(state, "ticket_id")
    run_id = _required_uuid(state, "run_id")
    ticket = await runtime.context.workflow_repository.begin_run(
        runtime.context.principal, ticket_id, run_id
    )
    return {
        "customer_message": ticket.customer_message,
        "action_id": str(ticket.action_id),
        "order_reference": ticket.order_reference,
        "messages": [HumanMessage(content=ticket.customer_message)],
        "pending_request": ticket.pending_request,
        "clarification": "",
        "order_conflict": False,
        "order_result": {},
        "policy_result": {},
        "proposed_refund_amount": "",
    }


async def classify_request(
    state: TicketAgentState,
    runtime: Runtime[TicketGraphContext],
) -> dict[str, Any]:
    config: RunnableConfig = get_config()
    message = state["customer_message"]
    classification: TicketClassification | None = None
    pending = state.get("pending_request") or {}
    slot_text = re.sub(r"^(?:订单号|订单)(?:是|为)?[:：]?\s*", "", message.strip()).strip(" 。.!！")
    order_slot = ORDER_REFERENCE_PATTERN.fullmatch(slot_text)
    amount_slot = re.fullmatch(r"(\d+(?:\.\d{1,2})?)\s*(?:元|块|CNY)?", slot_text)
    if pending and (order_slot or (amount_slot and Decimal(amount_slot.group(1)) > 0)):
        previous = TicketClassification.model_validate(pending)
        if previous.category is TicketCategory.REFUND:
            updates: dict[str, Any] = {}
            if order_slot:
                updates["order_reference"] = order_slot.group(0)
            elif amount_slot and Decimal(amount_slot.group(1)) > 0:
                updates["requested_refund_amount"] = Decimal(amount_slot.group(1))
                updates["full_refund_requested"] = False
            classification = TicketClassification.model_validate(
                {**previous.model_dump(), **updates}
            )
    if classification is None:
        classification = await runtime.context.reasoner.classify(
            message, state.get("order_reference"), config
        )
    category = classification.category
    risk_level = (
        RiskLevel.HIGH_RISK_WRITE if category is TicketCategory.REFUND else RiskLevel.READ_ONLY
    )
    order_reference = state.get("order_reference") or classification.order_reference
    explicit_references = {
        match.group(0).casefold() for match in ORDER_REFERENCE_PATTERN.finditer(message)
    }
    candidates = explicit_references | (
        {classification.order_reference.casefold()} if classification.order_reference else set()
    )
    conflict = (
        bool(
            state.get("order_reference")
            and any(candidate != state["order_reference"].casefold() for candidate in candidates)
        )
        or len(explicit_references) > 1
    )
    trusted_classification = classification.model_copy(
        update={"category": category, "order_reference": order_reference}
    )
    await runtime.context.workflow_repository.save_triage(
        runtime.context.principal.tenant_id,
        _required_uuid(state, "ticket_id"),
        _required_uuid(state, "run_id"),
        category,
        classification.priority,
        risk_level,
    )
    return {
        "classification": trusted_classification.model_dump(mode="json"),
        "risk_level": risk_level.value,
        "order_reference": order_reference,
        "clarification": (
            "一个工单只能处理一个订单。请为新订单创建工单；本轮没有查询或修改订单。"
            if conflict
            else ""
        ),
        "order_conflict": conflict,
    }


def route_after_classification(state: TicketAgentState) -> Literal["order", "policy", "clarify"]:
    if state.get("clarification"):
        return "clarify"
    return "order" if state.get("order_reference") else "policy"


def prepare_order_call(state: TicketAgentState) -> dict[str, Any]:
    order_reference = state.get("order_reference")
    if not order_reference:
        raise ValueError("Order Tool call requires order_reference")
    return {
        "order_tool_started_at": time(),
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": query_order.name,
                        "args": {"order_reference": order_reference},
                        "id": f"query-order-{state['run_id']}",
                        "type": "tool_call",
                    }
                ],
            )
        ],
    }


async def capture_order_result(
    state: TicketAgentState, runtime: Runtime[TicketGraphContext]
) -> dict[str, Any]:
    message = state["messages"][-1]
    if not isinstance(message, ToolMessage):
        raise TypeError("query_order node did not return a ToolMessage")
    result = _tool_payload(message)
    error_code = result.get("error_code")
    await runtime.context.workflow_repository.record_tool_result(
        runtime.context.principal.tenant_id,
        _required_uuid(state, "ticket_id"),
        _required_uuid(state, "run_id"),
        tool_name=query_order.name,
        succeeded=error_code != "DEPENDENCY_TIMEOUT",
        duration_ms=_elapsed_ms(state.get("order_tool_started_at")),
        details={"found": bool(result.get("found")), "error_code": error_code},
    )
    if result.get("found"):
        await runtime.context.workflow_repository.link_order(
            runtime.context.principal,
            _required_uuid(state, "ticket_id"),
            _required_uuid(state, "run_id"),
            result["order_reference"],
        )
    return {"order_result": result}


def prepare_policy_call(state: TicketAgentState) -> dict[str, Any]:
    return {
        "policy_tool_started_at": time(),
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": search_policy.name,
                        "args": {"query": state["customer_message"]},
                        "id": f"search-policy-{state['run_id']}",
                        "type": "tool_call",
                    }
                ],
            )
        ],
    }


async def capture_policy_result(
    state: TicketAgentState, runtime: Runtime[TicketGraphContext]
) -> dict[str, Any]:
    message = state["messages"][-1]
    if not isinstance(message, ToolMessage):
        raise TypeError("search_policy node did not return a ToolMessage")
    result = _tool_payload(message)
    error_code = result.get("error_code")
    await runtime.context.workflow_repository.record_tool_result(
        runtime.context.principal.tenant_id,
        _required_uuid(state, "ticket_id"),
        _required_uuid(state, "run_id"),
        tool_name=search_policy.name,
        succeeded=error_code != "DEPENDENCY_TIMEOUT",
        duration_ms=_elapsed_ms(state.get("policy_tool_started_at")),
        details={
            "evidence_count": len(result.get("evidence", [])),
            "error_code": error_code,
        },
    )
    return {"policy_result": result}


def _elapsed_ms(started_at: float | None) -> int:
    if started_at is None:
        return 0
    return max(0, round((time() - started_at) * 1000))


def plan_work(state: TicketAgentState) -> dict[str, Any]:
    classification = TicketClassification.model_validate(state["classification"])
    order_result = state.get("order_result")
    if classification.category in {
        TicketCategory.REFUND,
        TicketCategory.ORDER_STATUS,
    } and not state.get("order_reference"):
        return {
            "proposed_refund_amount": "",
            "clarification": "请提供需要处理的订单号，我不会猜测订单信息。",
        }
    if classification.category is not TicketCategory.REFUND or not order_result:
        return {"proposed_refund_amount": ""}
    if not order_result.get("found") or not order_result.get("order"):
        return {"proposed_refund_amount": ""}
    refundable = Decimal(order_result["order"]["refundable_amount"])
    requested = classification.requested_refund_amount
    if requested is None:
        if classification.full_refund_requested:
            requested = refundable
        else:
            return {
                "proposed_refund_amount": "",
                "clarification": "请明确需要退款的金额，或明确申请全额退款；目前没有创建退款审批。",
            }
    if classification.full_refund_requested and requested != refundable:
        return {
            "proposed_refund_amount": "",
            "clarification": "申请金额与全额退款不一致，请明确金额或重新申请全额退款。",
        }
    if requested <= 0 or requested > refundable:
        return {"proposed_refund_amount": ""}
    return {"proposed_refund_amount": str(requested)}


def route_work(state: TicketAgentState) -> Literal["approval", "answer"]:
    return "approval" if state.get("proposed_refund_amount") else "answer"


def _policy_evidence(state: TicketAgentState) -> list[Citation]:
    policy_result = state.get("policy_result") or {}
    return [Citation.model_validate(item) for item in policy_result.get("evidence", [])]


async def generate_grounded_answer(
    state: TicketAgentState,
    runtime: Runtime[TicketGraphContext],
) -> dict[str, Any]:
    config: RunnableConfig = get_config()
    if state.get("clarification"):
        answer = state["clarification"]
        return {"final_answer": answer, "messages": [AIMessage(content=answer)]}
    classification = TicketClassification.model_validate(state["classification"])
    order_result = state.get("order_result")
    if classification.category in {TicketCategory.ORDER_STATUS, TicketCategory.REFUND}:
        if not state.get("order_reference"):
            answer = "请提供需要查询的订单号，我不会猜测订单信息。"
            return {"final_answer": answer, "messages": [AIMessage(content=answer)]}
        if not order_result or not order_result.get("found"):
            answer = "在当前账户授权范围内未找到该订单，请核对订单号。"
            return {"final_answer": answer, "messages": [AIMessage(content=answer)]}
    if classification.category is TicketCategory.REFUND and order_result:
        refundable = Decimal(order_result["order"]["refundable_amount"])
        requested = classification.requested_refund_amount
        if refundable <= 0:
            answer = "订单当前没有可退款金额，因此没有创建退款审批。"
            return {"final_answer": answer, "messages": [AIMessage(content=answer)]}
        if requested is not None and requested > refundable:
            answer = f"申请金额超过当前可退款上限 {refundable:.2f}，没有创建退款审批。"
            return {"final_answer": answer, "messages": [AIMessage(content=answer)]}
    evidence = _policy_evidence(state)
    if classification.category is TicketCategory.POLICY and not evidence:
        answer = "当前没有检索到可引用的售后政策，我不会编造政策答案。"
    else:
        answer = await runtime.context.reasoner.answer(
            state["customer_message"], classification, order_result, evidence, config
        )
    return {"final_answer": answer, "messages": [AIMessage(content=answer)]}


async def finalize_ticket(
    state: TicketAgentState, runtime: Runtime[TicketGraphContext]
) -> dict[str, Any]:
    if state.get("clarification"):
        pending = (
            state["classification"]
            if state["classification"]["category"] == TicketCategory.REFUND.value
            else {}
        )
        if state.get("order_conflict"):
            pending = {}
        await runtime.context.workflow_repository.request_information(
            runtime.context.principal.tenant_id,
            _required_uuid(state, "ticket_id"),
            _required_uuid(state, "run_id"),
            state["final_answer"],
            pending,
        )
        return {}
    citations = [item.model_dump(mode="json") for item in _policy_evidence(state)]
    await runtime.context.workflow_repository.resolve_ticket(
        runtime.context.principal.tenant_id,
        _required_uuid(state, "ticket_id"),
        _required_uuid(state, "run_id"),
        state["final_answer"],
        citations,
    )
    return {}


async def create_pending_approval(
    state: TicketAgentState, runtime: Runtime[TicketGraphContext]
) -> dict[str, Any]:
    order_reference = state.get("order_reference")
    if not order_reference:
        raise ValueError("Refund approval requires order_reference")
    approval = await runtime.context.workflow_repository.create_pending_refund(
        runtime.context.principal,
        _required_uuid(state, "ticket_id"),
        _required_uuid(state, "run_id"),
        _required_uuid(state, "action_id"),
        order_reference,
        Decimal(state["proposed_refund_amount"]),
    )
    return {"approval_id": str(approval.id)}


def interrupt_for_approval(state: TicketAgentState) -> dict[str, Any]:
    resume_payload = interrupt(
        {
            "type": "refund_approval",
            "approval_id": state["approval_id"],
            "ticket_id": state["ticket_id"],
            "amount": state["proposed_refund_amount"],
            "message": "退款提案已保存，等待人工审批。",
        }
    )
    return {"resume_payload": resume_payload}


async def verify_approval_from_db(
    state: TicketAgentState, runtime: Runtime[TicketGraphContext]
) -> dict[str, Any]:
    approval = await runtime.context.workflow_repository.get_approval_for_resume(
        runtime.context.principal.tenant_id, UUID(state["approval_id"])
    )
    return {"resume_payload": {"database_status": approval["status"]}}


def route_approval(state: TicketAgentState) -> Literal["execute", "reject"]:
    status = ApprovalStatus(state["resume_payload"]["database_status"])
    if status is ApprovalStatus.APPROVED:
        return "execute"
    if status is ApprovalStatus.REJECTED:
        return "reject"
    raise ValueError(f"Cannot resume refund workflow from approval status {status.value}")


async def execute_refund_mock(
    state: TicketAgentState, runtime: Runtime[TicketGraphContext]
) -> dict[str, Any]:
    result = await runtime.context.workflow_repository.execute_approved_refund(
        runtime.context.principal.tenant_id,
        UUID(state["approval_id"]),
        _required_uuid(state, "run_id"),
    )
    return {"final_answer": result.message, "messages": [AIMessage(content=result.message)]}


async def generate_rejection_response(
    state: TicketAgentState, runtime: Runtime[TicketGraphContext]
) -> dict[str, Any]:
    message = await runtime.context.workflow_repository.resolve_rejected_refund(
        runtime.context.principal.tenant_id,
        UUID(state["approval_id"]),
        _required_uuid(state, "run_id"),
    )
    return {"final_answer": message, "messages": [AIMessage(content=message)]}


def build_ticketpilot_graph(checkpointer: BaseCheckpointSaver[Any] | None = None):
    builder = StateGraph(TicketAgentState, context_schema=TicketGraphContext)
    builder.add_node("load_ticket_context", load_ticket_context)
    builder.add_node("classify_request", classify_request)
    builder.add_node("prepare_order_call", prepare_order_call)
    builder.add_node("query_order", ToolNode([query_order]))
    builder.add_node("capture_order_result", capture_order_result)
    builder.add_node("prepare_policy_call", prepare_policy_call)
    builder.add_node("search_policy", ToolNode([search_policy]))
    builder.add_node("capture_policy_result", capture_policy_result)
    builder.add_node("plan_work", plan_work)
    builder.add_node("generate_grounded_answer", generate_grounded_answer)
    builder.add_node("finalize_ticket", finalize_ticket)
    builder.add_node("create_pending_approval", create_pending_approval)
    builder.add_node("interrupt_for_approval", interrupt_for_approval)
    builder.add_node("verify_approval_from_db", verify_approval_from_db)
    builder.add_node("execute_refund_mock", execute_refund_mock)
    builder.add_node("generate_rejection_response", generate_rejection_response)

    builder.add_edge(START, "load_ticket_context")
    builder.add_edge("load_ticket_context", "classify_request")
    builder.add_conditional_edges(
        "classify_request",
        route_after_classification,
        {
            "order": "prepare_order_call",
            "policy": "prepare_policy_call",
            "clarify": "generate_grounded_answer",
        },
    )
    builder.add_edge("prepare_order_call", "query_order")
    builder.add_edge("query_order", "capture_order_result")
    builder.add_edge("capture_order_result", "prepare_policy_call")
    builder.add_edge("prepare_policy_call", "search_policy")
    builder.add_edge("search_policy", "capture_policy_result")
    builder.add_edge("capture_policy_result", "plan_work")
    builder.add_conditional_edges(
        "plan_work",
        route_work,
        {"approval": "create_pending_approval", "answer": "generate_grounded_answer"},
    )
    builder.add_edge("generate_grounded_answer", "finalize_ticket")
    builder.add_edge("finalize_ticket", END)
    builder.add_edge("create_pending_approval", "interrupt_for_approval")
    builder.add_edge("interrupt_for_approval", "verify_approval_from_db")
    builder.add_conditional_edges(
        "verify_approval_from_db",
        route_approval,
        {"execute": "execute_refund_mock", "reject": "generate_rejection_response"},
    )
    builder.add_edge("execute_refund_mock", END)
    builder.add_edge("generate_rejection_response", END)
    return builder.compile(checkpointer=checkpointer, name="ticketpilot")
