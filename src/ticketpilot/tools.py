import asyncio
from dataclasses import dataclass
from typing import Any, cast

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from ticketpilot.domain import OrderLookupErrorCode, PolicyLookupErrorCode
from ticketpilot.orders import OrderReader
from ticketpilot.policies import PolicyRetriever
from ticketpilot.schemas import OrderLookupResult, PolicySearchResult, RequestPrincipal


@dataclass(frozen=True, kw_only=True)
class TicketPilotContext:
    principal: RequestPrincipal
    order_reader: OrderReader
    policy_retriever: PolicyRetriever | None = None
    order_timeout_seconds: float = 3.0
    policy_timeout_seconds: float = 3.0


async def query_order_func(
    order_reference: str,
    runtime: ToolRuntime[Any, Any],
) -> dict[str, Any]:
    """Query one authorized order by its user-visible reference."""
    context = cast(TicketPilotContext, runtime.context)
    try:
        async with asyncio.timeout(context.order_timeout_seconds):
            order = await context.order_reader.get_by_reference(context.principal, order_reference)
    except TimeoutError:
        result = OrderLookupResult(
            found=False,
            order_reference=order_reference,
            error_code=OrderLookupErrorCode.DEPENDENCY_TIMEOUT,
        )
    else:
        result = (
            OrderLookupResult(
                found=False,
                order_reference=order_reference,
                error_code=OrderLookupErrorCode.ORDER_NOT_FOUND,
            )
            if order is None
            else OrderLookupResult(
                found=True,
                order_reference=order_reference,
                order=order,
            )
        )
    return result.model_dump(mode="json")


query_order: BaseTool = tool("query_order")(query_order_func)


async def search_policy_func(
    query: str,
    runtime: ToolRuntime[Any, Any],
) -> dict[str, Any]:
    """Search the approved after-sales policy corpus and return fixed citations."""
    context = cast(TicketPilotContext, runtime.context)
    if context.policy_retriever is None:
        raise RuntimeError("Policy retriever is not configured")
    try:
        async with asyncio.timeout(context.policy_timeout_seconds):
            evidence = await context.policy_retriever.search(context.principal, query)
    except TimeoutError:
        result = PolicySearchResult(
            query=query,
            error_code=PolicyLookupErrorCode.DEPENDENCY_TIMEOUT,
        )
    else:
        result = (
            PolicySearchResult(
                query=query,
                error_code=PolicyLookupErrorCode.NO_RELEVANT_POLICY,
            )
            if not evidence
            else PolicySearchResult(query=query, evidence=evidence)
        )
    return result.model_dump(mode="json")


search_policy: BaseTool = tool("search_policy")(search_policy_func)
