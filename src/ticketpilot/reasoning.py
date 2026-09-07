import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from core import get_model, settings
from ticketpilot.domain import TicketCategory, TicketPriority
from ticketpilot.schemas import Citation, TicketClassification

ORDER_REFERENCE_PATTERN = re.compile(r"\b(?:TP-\d{4}|O-[A-Za-z0-9-]+)\b", re.IGNORECASE)
REFUND_TERMS = ("退款", "退钱", "退费", "refund")
POLICY_TERMS = ("政策", "规则", "条件", "时限", "期限", "policy")


class TicketReasoner(Protocol):
    async def classify(
        self,
        message: str,
        known_order_reference: str | None,
        config: RunnableConfig,
    ) -> TicketClassification: ...

    async def answer(
        self,
        message: str,
        classification: TicketClassification,
        order_result: dict[str, Any] | None,
        policy_evidence: list[Citation],
        config: RunnableConfig,
    ) -> str: ...


class DeterministicDemoReasoner:
    async def classify(
        self,
        message: str,
        known_order_reference: str | None,
        config: RunnableConfig,
    ) -> TicketClassification:
        del config
        normalized = message.casefold()
        reference_match = ORDER_REFERENCE_PATTERN.search(message)
        order_reference = known_order_reference or (
            reference_match.group(0).upper() if reference_match else None
        )
        if any(term in normalized for term in REFUND_TERMS):
            category = TicketCategory.REFUND
            priority = TicketPriority.HIGH
        elif order_reference is not None:
            category = TicketCategory.ORDER_STATUS
            priority = TicketPriority.NORMAL
        elif any(term in normalized for term in POLICY_TERMS):
            category = TicketCategory.POLICY
            priority = TicketPriority.NORMAL
        else:
            category = TicketCategory.OTHER
            priority = TicketPriority.NORMAL
        return TicketClassification(
            category=category,
            priority=priority,
            order_reference=order_reference,
            requested_refund_amount=(
                self._refund_amount(message, order_reference)
                if category is TicketCategory.REFUND
                else None
            ),
        )

    async def answer(
        self,
        message: str,
        classification: TicketClassification,
        order_result: dict[str, Any] | None,
        policy_evidence: list[Citation],
        config: RunnableConfig,
    ) -> str:
        del message, config
        evidence_text = "、".join(
            f"{item.title}（{item.chunk_id or item.source_id}）" for item in policy_evidence
        )
        if order_result and order_result.get("found") and order_result.get("order"):
            order = order_result["order"]
            answer = (
                f"订单 {order['order_reference']} 当前支付状态为 {order['payment_status']}，"
                f"履约状态为 {order['fulfillment_status']}。"
            )
            if order.get("carrier"):
                answer += f"承运商为 {order['carrier']}。"
            if order.get("estimated_delivery_at"):
                answer += f"预计送达时间为 {order['estimated_delivery_at']}。"
            if evidence_text:
                answer += f"参考依据：{evidence_text}。"
            return answer
        if classification.category is TicketCategory.POLICY and evidence_text:
            excerpts = " ".join(item.excerpt for item in policy_evidence if item.excerpt)
            return f"检索到的政策依据为：{excerpts} 参考：{evidence_text}。"
        return "当前证据不足，无法给出可靠结论。"

    @staticmethod
    def _refund_amount(message: str, order_reference: str | None) -> Decimal | None:
        amount_source = message
        if order_reference:
            amount_source = re.sub(re.escape(order_reference), "", amount_source, flags=re.I)
        matches = re.findall(r"(?<![\d.])(\d+(?:\.\d{1,2})?)(?![\d.])", amount_source)
        if not matches:
            return None
        try:
            amount = Decimal(matches[0])
        except InvalidOperation:
            return None
        return amount if amount > 0 else None


class LangChainTicketReasoner:
    async def classify(
        self,
        message: str,
        known_order_reference: str | None,
        config: RunnableConfig,
    ) -> TicketClassification:
        model_name = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)
        model = get_model(model_name)
        runnable = model.with_structured_output(TicketClassification)
        response = await runnable.ainvoke(
            [
                SystemMessage(
                    content=(
                        "Classify one TicketPilot after-sales request. Extract an order reference "
                        "and explicitly requested refund amount when present. Use REFUND only when "
                        "the user requests money back; use ORDER_STATUS for order or delivery facts; "
                        "use POLICY for policy-only questions; otherwise OTHER."
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "message": message,
                            "known_order_reference": known_order_reference,
                        },
                        ensure_ascii=False,
                    )
                ),
            ],
            config,
        )
        return TicketClassification.model_validate(response)

    async def answer(
        self,
        message: str,
        classification: TicketClassification,
        order_result: dict[str, Any] | None,
        policy_evidence: list[Citation],
        config: RunnableConfig,
    ) -> str:
        model_name = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)
        model = get_model(model_name)
        evidence = [item.model_dump(mode="json") for item in policy_evidence]
        response = await model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "You are TicketPilot. Answer in Chinese using only the supplied order facts "
                        "and policy evidence. Never invent an order state, amount, ETA, approval, or "
                        "citation. Clearly state when a fact or policy is unavailable."
                    )
                ),
                HumanMessage(
                    content=json.dumps(
                        {
                            "customer_message": message,
                            "classification": classification.model_dump(mode="json"),
                            "order_result": order_result,
                            "policy_evidence": evidence,
                        },
                        ensure_ascii=False,
                    )
                ),
            ],
            config,
        )
        if isinstance(response.content, str):
            return response.content
        return json.dumps(response.content, ensure_ascii=False)
