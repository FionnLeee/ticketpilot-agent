import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI

from core import get_model, settings
from schema.models import OpenAICompatibleName
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
        order_reference = reference_match.group(0) if reference_match else known_order_reference
        denied = bool(
            re.search(
                r"不(?:需要|要|用|想)?(?:退款|退钱|退费|退了)|不退了|取消退款|don't.*refund|no refund",
                normalized,
            )
        )
        policy_only = any(term in normalized for term in POLICY_TERMS)
        if denied:
            category = TicketCategory.ORDER_STATUS if "物流" in message else TicketCategory.OTHER
            priority = TicketPriority.NORMAL
        elif policy_only:
            category = TicketCategory.POLICY
            priority = TicketPriority.NORMAL
        elif any(term in normalized for term in REFUND_TERMS) or "退一部分" in message:
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
            full_refund_requested=(
                category is TicketCategory.REFUND
                and any(term in normalized for term in ("全额", "全部", "full refund"))
            ),
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
        if classification.category is TicketCategory.POLICY and evidence_text:
            excerpts = " ".join(item.excerpt for item in policy_evidence if item.excerpt)
            return f"检索到的政策依据为：{excerpts} 参考：{evidence_text}。"
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
        return "当前证据不足，无法给出可靠结论。"

    @staticmethod
    def _refund_amount(message: str, order_reference: str | None) -> Decimal | None:
        amount_source = message
        if order_reference:
            amount_source = re.sub(re.escape(order_reference), "", amount_source, flags=re.I)
        matches = re.findall(
            r"(?<![\d.\-])(\d+(?:\.\d{1,2})?)\s*(?:元|块|CNY|yuan)", amount_source, flags=re.I
        )
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
        if model_name == OpenAICompatibleName.OPENAI_COMPATIBLE:
            # Decimal's generated regex is unsupported by some compatible schema decoders.
            wire_schema = TicketClassification.model_json_schema()
            wire_schema["required"] = list(wire_schema["properties"])
            for field_schema in wire_schema["properties"].values():
                field_schema.pop("default", None)
            wire_schema["properties"]["requested_refund_amount"] = {
                "anyOf": [{"type": "number"}, {"type": "null"}],
                "description": "Explicit refund amount, positive with at most two decimal places.",
            }
            wire_schema["properties"]["full_refund_requested"]["description"] = (
                "True only when the user actively asks to refund the entire payment or all "
                "remaining refundable money; false for partial, policy-only, negated or cancelled."
            )
            runnable = cast(ChatOpenAI, model).with_structured_output(
                wire_schema, method="json_schema"
            )
        else:
            runnable = model.with_structured_output(TicketClassification)
        response = await runnable.ainvoke(
            [
                SystemMessage(
                    content=(
                        "Classify one TicketPilot after-sales request. Extract an order reference "
                        "and explicitly requested refund amount when present. Use REFUND only when "
                        "the user requests money back; use ORDER_STATUS for order or delivery facts; "
                        "use POLICY for policy-only questions; otherwise OTHER. Negated or cancelled "
                        "refund requests are not REFUND. Missing amount must stay null; never infer "
                        "an amount from order numbers, dates or an account balance. Set "
                        "full_refund_requested only for an explicit request to refund the full balance. "
                        "Extract the order explicitly mentioned in this message even if it differs "
                        "from known_order_reference. Do not infer a refund from a bare order number. "
                        "Apply these output rules to every request: "
                        "Always extract an explicitly present order reference, regardless of category. "
                        "If none is present, copy known_order_reference (including for cancellation "
                        "or OTHER); use null only when neither source supplies an order. "
                        "A message consisting only of an order reference is ORDER_STATUS, with that "
                        "reference extracted. Cancelled refunds without another question are OTHER. "
                        "Set full_refund_requested=true when the user actively asks to return all "
                        "paid money (全额退款, 全部退回); otherwise explicitly set it to false. "
                        "A policy question mentioning full refunds or a negated full-refund request "
                        "must not set this flag. For an explicit full refund without a numeric "
                        "requested amount, output true for the flag and null for the amount. "
                        "Return all classification fields explicitly; do not omit a field because "
                        "its schema has a default."
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
