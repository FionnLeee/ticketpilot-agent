"""Streaming generator for the synthetic multi-tenant after-sales history dataset.

Every row is derived from the manifest seed, so reruns produce identical ids and the
loader can dedupe on primary keys. Tickets, approvals, messages and audit events are
built per order, so order state (refunded amounts, statuses) and ticket history agree.
"""

import bisect
import hashlib
import json
import math
import random
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ticketpilot.paths import find_ancestor_path
from ticketpilot.policies import PolicyChunk, load_policy_corpus

DEFAULT_HISTORY_MANIFEST_PATH = find_ancestor_path(
    Path(__file__), "data", "ticketpilot", "history_manifest.json"
)
HISTORY_NAMESPACE = UUID("6f0c3d1e-8a4b-5c7d-9e2f-1a3b5c7d9e0f")
THREAD_PREFIX = "synthetic-history:"
ORDER_REFERENCE_PREFIX = "OH-"
CURRENCY = "CNY"

ORDER_COLUMNS = (
    "id",
    "tenant_id",
    "order_reference",
    "customer_id",
    "payment_status",
    "fulfillment_status",
    "paid_amount",
    "refundable_amount",
    "currency",
    "carrier",
    "tracking_number",
    "estimated_delivery_at",
    "version",
    "created_at",
    "updated_at",
)
TICKET_COLUMNS = (
    "id",
    "tenant_id",
    "customer_id",
    "order_pk",
    "thread_id",
    "status",
    "processing_result",
    "category",
    "priority",
    "risk_level",
    "subject",
    "resolution_summary",
    "pending_request",
    "triaged_at",
    "resolved_at",
    "version",
    "created_at",
    "updated_at",
    "active_run_id",
    "trigger_message_id",
    "run_started",
    "create_request_actor_id",
    "create_idempotency_key",
    "create_request_hash",
)
MESSAGE_COLUMNS = (
    "id",
    "tenant_id",
    "ticket_id",
    "run_id",
    "role",
    "content",
    "citations",
    "idempotency_key",
    "created_at",
)
APPROVAL_COLUMNS = (
    "id",
    "tenant_id",
    "ticket_id",
    "run_id",
    "action_id",
    "action_type",
    "action_payload",
    "status",
    "requested_by",
    "decided_by",
    "decision_reason",
    "idempotency_key",
    "requested_at",
    "decided_at",
    "executed_at",
)
EVENT_COLUMNS = (
    "id",
    "tenant_id",
    "ticket_id",
    "run_id",
    "approval_id",
    "actor_type",
    "actor_id",
    "event_type",
    "node_name",
    "tool_name",
    "outcome",
    "details",
    "occurred_at",
)


@dataclass(frozen=True)
class TenantProfile:
    code: str
    weight: float
    ticket_rate: float
    refund_share: float
    orders_per_customer: float
    carriers: tuple[str, ...]


@dataclass(frozen=True)
class HistoryConfig:
    schema_version: int
    generator_module: str
    dataset_id: str
    generator_version: int
    generator_seed: int
    base_time: datetime
    history_days: int
    order_count: int
    tenant_prefix: str
    tenants: tuple[TenantProfile, ...]
    policy_manifest_path: Path
    contains_real_personal_data: bool
    license: str
    provenance: str

    def tenant_id(self, profile: TenantProfile) -> str:
        return f"{self.tenant_prefix}{profile.code}"

    def tenant_ids(self) -> list[str]:
        return [self.tenant_id(profile) for profile in self.tenants]

    def tenant_order_counts(self) -> list[int]:
        # Largest-remainder allocation so the counts always sum to order_count exactly.
        raw = [self.order_count * profile.weight for profile in self.tenants]
        counts = [int(value) for value in raw]
        remainders = sorted(
            range(len(raw)), key=lambda index: raw[index] - counts[index], reverse=True
        )
        for index in remainders[: self.order_count - sum(counts)]:
            counts[index] += 1
        return counts

    def describe(self) -> dict[str, Any]:
        policy_manifest = json.loads(self.policy_manifest_path.read_text(encoding="utf-8"))
        policy_manifest_canonical = json.dumps(
            policy_manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "generator_module": self.generator_module,
            "generator_version": self.generator_version,
            "generator_seed": self.generator_seed,
            "base_time": self.base_time.isoformat(),
            "history_days": self.history_days,
            "order_count": self.order_count,
            "tenant_prefix": self.tenant_prefix,
            "tenants": [asdict(profile) for profile in self.tenants],
            "policy_manifest": self.policy_manifest_path.name,
            "policy_manifest_sha256": hashlib.sha256(
                policy_manifest_canonical.encode("utf-8")
            ).hexdigest(),
            "thread_prefix": THREAD_PREFIX,
            "order_reference_prefix": ORDER_REFERENCE_PREFIX,
            "synthetic": True,
            "contains_real_personal_data": self.contains_real_personal_data,
            "license": self.license,
            "provenance": self.provenance,
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.describe(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_history_config(path: Path = DEFAULT_HISTORY_MANIFEST_PATH) -> HistoryConfig:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    base_time = datetime.fromisoformat(payload["base_time"])
    if base_time.tzinfo is None:
        raise ValueError("History manifest base_time must include a timezone")
    tenants = tuple(
        TenantProfile(
            code=str(item["code"]),
            weight=float(item["weight"]),
            ticket_rate=float(item["ticket_rate"]),
            refund_share=float(item["refund_share"]),
            orders_per_customer=float(item["orders_per_customer"]),
            carriers=tuple(item["carriers"]),
        )
        for item in payload["tenants"]
    )
    config = HistoryConfig(
        schema_version=int(payload["schema_version"]),
        generator_module=payload["generator_module"],
        dataset_id=payload["dataset_id"],
        generator_version=int(payload["generator_version"]),
        generator_seed=int(payload["generator_seed"]),
        base_time=base_time,
        history_days=int(payload["history_days"]),
        order_count=int(payload["order_count"]),
        tenant_prefix=payload["tenant_prefix"],
        tenants=tenants,
        policy_manifest_path=path.parent / payload["policy_manifest"],
        contains_real_personal_data=bool(payload["contains_real_personal_data"]),
        license=payload["license"],
        provenance=payload["provenance"],
    )
    validate_history_config(config)
    return config


def validate_history_config(config: HistoryConfig) -> None:
    if config.schema_version != 1 or config.generator_module != "ticketpilot.history_data":
        raise ValueError("Unsupported history manifest schema or generator module")
    if config.contains_real_personal_data:
        raise ValueError("The synthetic history manifest must not contain real personal data")
    if not config.tenants:
        raise ValueError("History manifest needs at least one tenant")
    if len({profile.code for profile in config.tenants}) != len(config.tenants):
        raise ValueError("History manifest tenant codes must be unique")
    if abs(sum(profile.weight for profile in config.tenants) - 1.0) > 1e-6:
        raise ValueError("History manifest tenant weights must sum to 1")
    if config.order_count < len(config.tenants) or config.history_days < 30:
        raise ValueError("History manifest needs order_count >= tenants and history_days >= 30")
    for profile in config.tenants:
        if not 0 < profile.ticket_rate <= 1 or not 0 < profile.refund_share <= 1:
            raise ValueError(f"Tenant {profile.code} rates must be in (0, 1]")
        if profile.orders_per_customer < 1 or not profile.carriers:
            raise ValueError(f"Tenant {profile.code} needs orders_per_customer >= 1 and carriers")


def with_order_count(config: HistoryConfig, order_count: int) -> HistoryConfig:
    """Smaller variants get their own dataset id and tenant namespace, so they never mix."""
    if order_count == config.order_count:
        return config
    return replace(
        config,
        order_count=order_count,
        dataset_id=f"{config.dataset_id}-{order_count}",
        tenant_prefix=f"{config.tenant_prefix}{order_count}-",
    )


@dataclass
class HistoryBatch:
    orders: list[tuple[Any, ...]] = field(default_factory=list)
    tickets: list[tuple[Any, ...]] = field(default_factory=list)
    messages: list[tuple[Any, ...]] = field(default_factory=list)
    approvals: list[tuple[Any, ...]] = field(default_factory=list)
    events: list[tuple[Any, ...]] = field(default_factory=list)

    def row_counts(self) -> dict[str, int]:
        return {
            "orders": len(self.orders),
            "tickets": len(self.tickets),
            "ticket_messages": len(self.messages),
            "approvals": len(self.approvals),
            "audit_events": len(self.events),
        }


# (payment_status, fulfillment_status) templates per order age bucket, with weights.
_STATE_BUCKETS: tuple[tuple[int, tuple[tuple[str, str, float], ...]], ...] = (
    (
        1,
        (
            ("PENDING", "PENDING", 0.35),
            ("PAID", "PROCESSING", 0.45),
            ("PAID", "SHIPPED", 0.12),
            ("CANCELLED", "CANCELLED", 0.08),
        ),
    ),
    (
        7,
        (
            ("PENDING", "PENDING", 0.03),
            ("PAID", "PROCESSING", 0.12),
            ("PAID", "SHIPPED", 0.50),
            ("PAID", "DELIVERED", 0.25),
            ("CANCELLED", "CANCELLED", 0.07),
            ("REFUNDED", "CANCELLED", 0.03),
        ),
    ),
    (
        30,
        (
            ("PENDING", "PENDING", 0.02),
            ("PAID", "SHIPPED", 0.08),
            ("PAID", "DELIVERED", 0.70),
            ("PARTIALLY_REFUNDED", "DELIVERED", 0.06),
            ("REFUNDED", "DELIVERED", 0.05),
            ("CANCELLED", "CANCELLED", 0.06),
            ("REFUNDED", "CANCELLED", 0.03),
        ),
    ),
    (
        10**9,
        (
            ("PENDING", "PENDING", 0.02),
            ("PAID", "SHIPPED", 0.01),
            ("PAID", "DELIVERED", 0.75),
            ("PARTIALLY_REFUNDED", "DELIVERED", 0.07),
            ("REFUNDED", "DELIVERED", 0.06),
            ("CANCELLED", "CANCELLED", 0.06),
            ("REFUNDED", "CANCELLED", 0.03),
        ),
    ),
)

_WEEKDAY_FACTORS = (1.0, 0.95, 0.95, 1.0, 1.1, 1.25, 1.2)
_PROMO_DAYS = {(6, 18): 3.0, (11, 11): 4.0, (12, 12): 2.5}
_HOUR_WEIGHTS = (
    1, 1, 1, 1, 1, 1, 2, 4, 7, 10, 12, 12, 10, 9, 9, 9, 9, 9, 10, 12, 14, 14, 10, 5,
)  # fmt: skip

_CATEGORY_TRIAGE = {
    "ORDER_STATUS": ("NORMAL", "READ_ONLY"),
    "REFUND": ("HIGH", "HIGH_RISK_WRITE"),
    "POLICY": ("NORMAL", "READ_ONLY"),
    "OTHER": ("NORMAL", "READ_ONLY"),
}

_ORDER_STATUS_QUESTIONS = (
    "查询订单 {ref} 的物流",
    "订单 {ref} 到哪了？",
    "{ref} 什么时候能送到",
    "帮我看下 {ref} 的配送进度",
    "{ref} 显示已发货，几天了还没动",
)
_REFUND_AMOUNT_QUESTIONS = (
    "订单 {ref} 我要退款 {amount} 元",
    "{ref} 申请退款 {amount} 元",
    "我想退 {amount} 元，订单 {ref}",
)
_REFUND_FULL_QUESTIONS = ("订单 {ref} 我要全额退款", "{ref} 全部退掉，不想要了")
_REFUND_MISSING_QUESTIONS = ("订单 {ref} 我要退款", "{ref} 能退吗，想退一部分", "{ref} 退钱")
_CANCEL_REFUND_QUESTIONS = ("取消订单 {ref} 的退款申请", "订单 {ref} 不退了，麻烦取消退款")
_INFO_REPLY_QUESTIONS = ("订单号是 {ref}", "补充一下，订单是 {ref}")
_UNKNOWN_REFERENCE_QUESTIONS = ("查询订单 {ref} 的物流", "订单 {ref} 到哪了？")
_POLICY_QUESTION_SUFFIXES = ("是怎么规定的？", "的规则是什么", "有什么条件")
_UNSUPPORTED_POLICY_QUESTIONS = (
    "海外仓直邮的关税政策是什么",
    "企业采购的账期规则",
    "预售商品的定金政策",
    "直播间专属价的价保规则",
)
_REJECTION_REASONS = (
    "商品已签收超过退款窗口",
    "退款金额与售后凭证不符",
    "订单已完成换货处理",
    "需要客户先寄回商品",
)
_ORDER_STATUS_ANSWER_LIMIT = "申请金额超过当前可退款上限 {limit}，没有创建退款审批。"
_ZERO_BALANCE_ANSWER = "订单当前没有可退款金额，因此没有创建退款审批。"
_MISSING_AMOUNT_ANSWER = "请明确需要退款的金额，或明确申请全额退款；目前没有创建退款审批。"
_UNKNOWN_ORDER_ANSWER = "在当前账户授权范围内未找到该订单，请核对订单号。"
_DEPENDENCY_ANSWER = "订单服务暂时不可用，本轮没有确认订单状态。请稍后使用新的请求重试。"
_NO_POLICY_ANSWER = "当前没有检索到可引用的售后政策，本轮无法给出有依据的结论，请转人工核查。"
_REJECTED_ANSWER = "退款申请未获批准，本次未执行任何退款。"
_BLOCKED_ANSWER = "审批后订单状态已变化，Mock 退款被安全阻断。"


def _weighted_index(rng: random.Random, cumulative: list[float]) -> int:
    return bisect.bisect(cumulative, rng.random() * cumulative[-1])


def _cumulative(weights: list[float]) -> list[float]:
    total = 0.0
    cumulative = []
    for weight in weights:
        total += weight
        cumulative.append(total)
    return cumulative


def _money(value: float) -> Decimal:
    return Decimal(f"{value:.2f}")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class _Calendar:
    """Order arrival pattern: mild growth, weekly rhythm and promotion spikes."""

    def __init__(self, config: HistoryConfig) -> None:
        self.base_time = config.base_time
        base_date = config.base_time.date()
        weights = []
        for offset in range(config.history_days, 0, -1):
            day = base_date - timedelta(days=offset)
            trend = 0.7 + 0.6 * (1 - offset / config.history_days)
            promo = _PROMO_DAYS.get((day.month, day.day), 1.0)
            weights.append(trend * _WEEKDAY_FACTORS[day.weekday()] * promo)
        self.day_cumulative = _cumulative(weights)
        self.hour_cumulative = _cumulative([float(weight) for weight in _HOUR_WEIGHTS])
        self.first_day_start = datetime.combine(
            base_date - timedelta(days=config.history_days),
            datetime.min.time(),
            tzinfo=config.base_time.tzinfo,
        )

    def sample(self, rng: random.Random) -> datetime:
        day_index = _weighted_index(rng, self.day_cumulative)
        hour = _weighted_index(rng, self.hour_cumulative)
        seconds = day_index * 86400 + hour * 3600 + rng.random() * 3600
        return self.first_day_start + timedelta(seconds=seconds)


@dataclass
class _Order:
    id: UUID
    reference: str
    customer_id: str
    payment_status: str
    fulfillment_status: str
    paid_amount: Decimal
    refundable_amount: Decimal
    carrier: str | None
    tracking_number: str | None
    estimated_delivery_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int = 1
    age_days: float = 0.0


class _TenantGenerator:
    def __init__(
        self,
        config: HistoryConfig,
        profile: TenantProfile,
        order_count: int,
        calendar: _Calendar,
        policy_chunks: tuple[PolicyChunk, ...],
    ) -> None:
        self.config = config
        self.profile = profile
        self.tenant_id = config.tenant_id(profile)
        self.calendar = calendar
        self.base_time = config.base_time
        self.rng = random.Random(f"{config.generator_seed}:{config.dataset_id}:{self.tenant_id}")
        self.customer_count = max(20, round(order_count / profile.orders_per_customer))
        self.approvers = [f"approver-hist-{profile.code}-{index}" for index in range(1, 4)]
        self.policy_chunks = [
            chunk
            for chunk in policy_chunks
            if "*" in chunk.tenant_ids or self.tenant_id in chunk.tenant_ids
        ]
        self.ticket_seq = 0
        self.state_buckets = [
            (limit, templates, _cumulative([weight for _, _, weight in templates]))
            for limit, templates in _STATE_BUCKETS
        ]
        refund_scale = profile.refund_share / 0.33
        self.paid_templates = self._template_table(
            [
                ("ORDER_STATUS", 0.50),
                ("UNKNOWN_ORDER", 0.03),
                ("DEPENDENCY_FAILED", 0.015),
                ("POLICY", 0.10),
                ("NO_POLICY", 0.015),
                ("CANCEL_REFUND", 0.04),
                ("REFUND_MISSING_AMOUNT", 0.02 * refund_scale),
                ("REFUND_MISSING_THEN_PENDING", 0.02 * refund_scale),
                ("REFUND_PENDING", 0.03 * refund_scale),
                ("REFUND_REJECTED", 0.06 * refund_scale),
                ("REFUND_OVER_LIMIT", 0.04 * refund_scale),
            ]
        )
        self.unpaid_templates = self._template_table(
            [
                ("ORDER_STATUS", 0.55),
                ("UNKNOWN_ORDER", 0.03),
                ("DEPENDENCY_FAILED", 0.01),
                ("POLICY", 0.15),
                ("NO_POLICY", 0.02),
                ("CANCEL_REFUND", 0.06),
                ("REFUND_ZERO_BALANCE", 0.18),
            ]
        )
        self.followup_templates = self._template_table([("ORDER_STATUS", 0.7), ("POLICY", 0.3)])
        self.detached_templates = self._template_table([("POLICY", 0.85), ("NO_POLICY", 0.15)])

    @staticmethod
    def _template_table(items: list[tuple[str, float]]) -> tuple[list[str], list[float]]:
        return [name for name, _ in items], _cumulative([weight for _, weight in items])

    def _pick(self, table: tuple[list[str], list[float]]) -> str:
        names, cumulative = table
        return names[_weighted_index(self.rng, cumulative)]

    def _uuid(self, kind: str, key: str) -> UUID:
        return uuid5(HISTORY_NAMESPACE, f"{self.config.dataset_id}:{self.tenant_id}:{kind}:{key}")

    def _customer(self) -> str:
        index = int(self.customer_count * self.rng.random() ** 1.7) + 1
        return f"customer-hist-{self.profile.code}-{index:06d}"

    def _lognormal_delay(self, median_seconds: float, sigma: float) -> timedelta:
        return timedelta(seconds=self.rng.lognormvariate(math.log(median_seconds), sigma))

    # --- orders ---------------------------------------------------------------------

    def emit_order(self, seq: int, batch: HistoryBatch) -> None:
        rng = self.rng
        created_at = self.calendar.sample(rng)
        age_days = (self.base_time - created_at).total_seconds() / 86400
        payment = fulfillment = ""
        for limit, templates, cumulative in self.state_buckets:
            if age_days < limit:
                payment, fulfillment, _ = templates[_weighted_index(rng, cumulative)]
                break
        amount = _money(min(50000.0, max(5.0, rng.lognormvariate(math.log(180.0), 0.9))))
        if payment == "PAID" and fulfillment == "DELIVERED" and rng.random() < 0.01:
            amount = Decimal("0.00")
        if payment == "PAID":
            refundable = amount
        elif payment == "PARTIALLY_REFUNDED":
            refunded = _money(float(amount) * rng.uniform(0.1, 0.6))
            refundable = amount - refunded if Decimal("0.00") < refunded < amount else amount
            if refundable == amount:
                payment = "PAID"
        else:
            refundable = Decimal("0.00")

        carrier = tracking = eta = None
        if fulfillment in {"SHIPPED", "DELIVERED"}:
            carrier = rng.choice(self.profile.carriers)
            tracking = f"TR{self.profile.code}{seq:010d}"
            eta = created_at + timedelta(days=rng.randint(2, 6), hours=rng.randint(0, 23))
        updated_at = created_at + timedelta(hours=rng.uniform(0.5, 72))
        if updated_at > self.base_time:
            updated_at = created_at + (self.base_time - created_at) / 2
        order = _Order(
            id=self._uuid("order", str(seq)),
            reference=f"{ORDER_REFERENCE_PREFIX}{seq:07d}",
            customer_id=self._customer(),
            payment_status=payment,
            fulfillment_status=fulfillment,
            paid_amount=amount,
            refundable_amount=refundable,
            carrier=carrier,
            tracking_number=tracking,
            estimated_delivery_at=eta,
            created_at=created_at,
            updated_at=updated_at,
            age_days=age_days,
        )
        self._emit_tickets(order, batch)
        batch.orders.append(
            (
                order.id,
                self.tenant_id,
                order.reference,
                order.customer_id,
                order.payment_status,
                order.fulfillment_status,
                order.paid_amount,
                order.refundable_amount,
                CURRENCY,
                order.carrier,
                order.tracking_number,
                order.estimated_delivery_at,
                order.version,
                order.created_at,
                order.updated_at,
            )
        )

    # --- ticket selection ------------------------------------------------------------

    def _emit_tickets(self, order: _Order, batch: HistoryBatch) -> None:
        rng = self.rng
        profile = self.profile
        if order.age_days * 24 < 1:
            return
        refunded_state = order.payment_status in {"PARTIALLY_REFUNDED", "REFUNDED"}
        template = None
        if refunded_state and order.fulfillment_status == "DELIVERED":
            if rng.random() < profile.refund_share * 0.9:
                blocked = order.payment_status == "REFUNDED" and rng.random() < 0.03
                template = "REFUND_BLOCKED" if blocked else "REFUND_EXECUTED"
            elif rng.random() < profile.ticket_rate * 0.5:
                template = self._pick(self.followup_templates)
        elif rng.random() < profile.ticket_rate:
            if order.refundable_amount > 0:
                template = self._pick(self.paid_templates)
                if template in {"REFUND_PENDING", "REFUND_MISSING_THEN_PENDING"}:
                    if order.age_days >= 14:
                        template = "REFUND_REJECTED"
                elif template == "REFUND_REJECTED" and order.age_days < 1:
                    template = "REFUND_PENDING"
            else:
                template = self._pick(self.unpaid_templates)
        if template is not None:
            self._emit_ticket(order, template, batch)
            if rng.random() < 0.06:
                self._emit_ticket(order, self._pick(self.followup_templates), batch)
        if rng.random() < 0.02:
            self._emit_ticket(order, self._pick(self.detached_templates), batch, detached=True)

    # --- ticket assembly --------------------------------------------------------------

    def _emit_ticket(
        self, order: _Order, template: str, batch: HistoryBatch, detached: bool = False
    ) -> None:
        self.ticket_seq += 1
        builder = _TicketBuilder(self, order, template, self.ticket_seq, detached)
        builder.build(batch)


class _TicketBuilder:
    """Assembles one ticket with its runs, messages, approval and audit trail."""

    def __init__(
        self,
        tenant: _TenantGenerator,
        order: _Order,
        template: str,
        seq: int,
        detached: bool,
    ) -> None:
        self.tenant = tenant
        self.rng = tenant.rng
        self.order = order
        self.template = template
        self.seq = seq
        self.detached = detached
        self.tenant_id = tenant.tenant_id
        self.ticket_id = tenant._uuid("ticket", str(seq))
        self.customer_id = tenant._customer() if detached else order.customer_id
        self.messages: list[tuple[Any, ...]] = []
        self.events: list[tuple[Any, ...]] = []
        self.approval: tuple[Any, ...] | None = None
        self.clock = order.created_at
        self.version = 1
        self.run_count = 0
        self.status: str | None = None
        self.result: str | None = None
        self.summary: str | None = None
        self.category = self.priority = self.risk = ""
        self.pending: dict[str, Any] = {}
        self.triaged_at: datetime | None = None
        self.resolved_at: datetime | None = None
        self.approval_id: UUID | None = None
        self.approval_run_id: UUID | None = None
        self.approval_action_id: UUID | None = None
        self.approval_amount = Decimal("0.00")
        self.approval_requested_at = order.created_at
        self.approver: str | None = None
        self.decision_reason: str | None = None
        self.decided_at: datetime | None = None
        self.executed_at: datetime | None = None

    # -- primitives --

    def _id(self, kind: str, key: str) -> UUID:
        return self.tenant._uuid(kind, f"{self.seq}:{key}")

    def _advance(self, seconds: float) -> datetime:
        self.clock += timedelta(seconds=seconds)
        return self.clock

    def _wait(self, median_seconds: float, sigma: float, reserve_seconds: float = 300) -> None:
        """Human-scale delay, clamped so the story ends before the snapshot time."""
        delay = self.rng.lognormvariate(math.log(median_seconds), sigma)
        latest = (self.tenant.base_time - self.clock).total_seconds() - reserve_seconds
        self._advance(max(1.0, min(delay, latest)))

    def _event(
        self,
        run_id: UUID | None,
        event_type: str,
        outcome: str,
        actor_type: str,
        *,
        actor_id: str | None = None,
        approval_id: UUID | None = None,
        node_name: str | None = None,
        tool_name: str | None = None,
        details: dict[str, Any] | None = None,
        seconds: float = 0.02,
    ) -> None:
        occurred_at = self._advance(seconds)
        payload = {"synthetic": True, **(details or {})}
        self.events.append(
            (
                self._id("event", str(len(self.events))),
                self.tenant_id,
                self.ticket_id,
                run_id,
                approval_id,
                actor_type,
                actor_id,
                event_type,
                node_name,
                tool_name,
                outcome,
                _json(payload),
                occurred_at,
            )
        )

    def _message(
        self, run_id: UUID, role: str, content: str, citations: list[dict[str, Any]]
    ) -> UUID:
        message_id = self._id("message", str(len(self.messages)))
        self.messages.append(
            (
                message_id,
                self.tenant_id,
                self.ticket_id,
                run_id,
                role,
                content,
                json.dumps(citations, ensure_ascii=False, separators=(",", ":")),
                None,
                self.clock,
            )
        )
        return message_id

    def _citation(self, chunk: PolicyChunk) -> dict[str, Any]:
        return {
            "uri": None,
            "title": chunk.title,
            "excerpt": chunk.content,
            "chunk_id": chunk.chunk_id,
            "source_id": chunk.source_id,
        }

    def _order_answer(self, citations: list[dict[str, Any]]) -> str:
        order = self.order
        answer = (
            f"订单 {order.reference} 当前支付状态为 {order.payment_status}，"
            f"履约状态为 {order.fulfillment_status}。"
        )
        if order.carrier:
            answer += f"承运商为 {order.carrier}。"
        if order.estimated_delivery_at:
            answer += (
                f"预计送达时间为 {order.estimated_delivery_at.strftime('%Y-%m-%dT%H:%M:%SZ')}。"
            )
        if citations:
            refs = "、".join(f"{item['title']}（{item['chunk_id']}）" for item in citations)
            answer += f"参考依据：{refs}。"
        return answer

    def _tracking_chunk(self) -> PolicyChunk:
        return next(c for c in self.tenant.policy_chunks if c.chunk_id == "order-tracking")

    def _random_chunks(self, count: int) -> list[PolicyChunk]:
        return self.rng.sample(self.tenant.policy_chunks, count)

    # -- run scaffolding --

    def _start_run(self, content: str, first: bool) -> tuple[UUID, UUID]:
        self.run_count += 1
        run_id = self._id("run", str(self.run_count))
        message_id = self._message(run_id, "CUSTOMER", content, [])
        if first:
            self._event(
                None,
                "TICKET_CREATED",
                "SUCCEEDED",
                "CUSTOMER",
                actor_id=self.customer_id,
                details={"order_linked": not self.detached and self.template != "UNKNOWN_ORDER"},
                seconds=0.01,
            )
        self._event(
            run_id,
            "RUN_STARTED",
            "STARTED",
            "CUSTOMER",
            actor_id=self.customer_id,
            node_name="load_ticket_context",
            seconds=self.rng.uniform(0.05, 0.4),
        )
        self.version += 1
        return run_id, message_id

    def _triage(self, run_id: UUID, category: str) -> None:
        priority, risk = _CATEGORY_TRIAGE[category]
        self._event(
            run_id,
            "TICKET_TRIAGED",
            "SUCCEEDED",
            "AGENT",
            node_name="classify_request",
            details={"category": category, "priority": priority, "risk_level": risk},
            seconds=self.rng.lognormvariate(math.log(1.2), 0.5),
        )
        self.category, self.priority, self.risk = category, priority, risk
        self.triaged_at = self.clock
        self.version += 1

    def _tool_order(self, run_id: UUID, found: bool = True, timeout: bool = False) -> None:
        if timeout:
            details: dict[str, Any] = {
                "found": False,
                "error_code": "DEPENDENCY_TIMEOUT",
                "duration_ms": 5000,
            }
            self._event(
                run_id, "TOOL_FAILED", "FAILED", "SYSTEM",
                node_name="query_order", tool_name="query_order", details=details, seconds=5.0,
            )  # fmt: skip
            return
        duration = self.rng.randint(3, 40)
        details = {
            "found": found,
            "error_code": None if found else "ORDER_NOT_FOUND",
            "duration_ms": duration,
        }
        self._event(
            run_id, "TOOL_SUCCEEDED", "SUCCEEDED", "SYSTEM",
            node_name="query_order", tool_name="query_order", details=details,
            seconds=duration / 1000,
        )  # fmt: skip

    def _tool_policy(self, run_id: UUID, evidence_count: int) -> None:
        duration = self.rng.randint(2, 25)
        self._event(
            run_id, "TOOL_SUCCEEDED", "SUCCEEDED", "SYSTEM",
            node_name="search_policy", tool_name="search_policy",
            details={"error_code": None, "duration_ms": duration, "evidence_count": evidence_count},
            seconds=duration / 1000,
        )  # fmt: skip

    def _finalize_answered(
        self, run_id: UUID, answer: str, citations: list[dict[str, Any]]
    ) -> None:
        self._advance(self.rng.lognormvariate(math.log(1.5), 0.5))
        self._message(run_id, "AGENT", answer, citations)
        self._event(
            run_id, "TICKET_RESOLVED", "SUCCEEDED", "AGENT", node_name="finalize_ticket",
            details={"citation_count": len(citations), "processing_result": "ANSWERED"},
            seconds=0.01,
        )  # fmt: skip
        self.status, self.result = "RESOLVED", "ANSWERED"
        self.summary = answer
        self.resolved_at = self.clock
        self.version += 1

    def _finalize_needs_input(self, run_id: UUID, answer: str, pending: dict[str, Any]) -> None:
        self._advance(self.rng.lognormvariate(math.log(1.2), 0.5))
        self._message(run_id, "AGENT", answer, [])
        self._event(
            run_id, "TICKET_INFORMATION_REQUESTED", "SUCCEEDED", "AGENT",
            node_name="finalize_ticket", details={"processing_result": "NEEDS_INPUT"}, seconds=0.01,
        )  # fmt: skip
        self.status, self.result = "WAITING_INFORMATION", "NEEDS_INPUT"
        self.summary = answer
        self.pending = pending
        self.version += 1

    def _finalize_failed(self, run_id: UUID, result: str, error_code: str, answer: str) -> None:
        self._advance(self.rng.uniform(0.2, 0.8))
        self._message(run_id, "AGENT", answer, [])
        self._event(
            run_id, result, "FAILED", "SYSTEM", node_name="finalize_ticket",
            details={"processing_result": result, "error_code": error_code}, seconds=0.01,
        )  # fmt: skip
        self.status, self.result = "FAILED", result
        self.summary = answer
        self.version += 1

    def _request_approval(self, run_id: UUID, message_id: UUID, amount: Decimal) -> UUID:
        approval_id = self._id("approval", "1")
        self._advance(self.rng.uniform(0.1, 0.5))
        self.approval_requested_at = self.clock
        self.approval_amount = amount
        self.approval_id = approval_id
        self.approval_run_id = run_id
        self.approval_action_id = message_id
        self._event(
            run_id, "REFUND_APPROVAL_REQUESTED", "BLOCKED", "AGENT",
            approval_id=approval_id, node_name="create_pending_approval",
            details={
                "amount": str(amount),
                "currency": CURRENCY,
                "action_id": str(message_id),
                "processing_result": "WAITING_APPROVAL",
            },
            seconds=0.01,
        )  # fmt: skip
        self.status, self.result = "WAITING_APPROVAL", "WAITING_APPROVAL"
        self.version += 1
        return approval_id

    def _decide_approval(self, decision: str) -> UUID:
        """Approval decision arrives hours later on a fresh run, mirroring the API path."""
        self.run_count += 1
        run_id = self._id("run", str(self.run_count))
        self._wait(3 * 3600, 1.2, reserve_seconds=120)
        self.approver = self.rng.choice(self.tenant.approvers)
        self.decision_reason = (
            "核对订单与售后凭证，同意退款"
            if decision == "APPROVE"
            else self.rng.choice(_REJECTION_REASONS)
        )
        self._event(
            run_id,
            "RUN_STARTED",
            "STARTED",
            "STAFF",
            actor_id=self.approver,
            node_name="load_ticket_context",
            seconds=self.rng.uniform(0.05, 0.4),
        )
        self._event(
            run_id, "APPROVAL_DECIDED", "SUCCEEDED", "STAFF", actor_id=self.approver,
            approval_id=self.approval_id, node_name="approval_api",
            details={"decision": decision, "reason": self.decision_reason}, seconds=0.01,
        )  # fmt: skip
        self.decided_at = self.clock
        self.version += 1
        return run_id

    def _write_approval(self, status: str, executed: bool) -> None:
        order = self.order
        decided = status != "PENDING"
        self.approval = (
            self.approval_id,
            self.tenant_id,
            self.ticket_id,
            self.approval_run_id,
            self.approval_action_id,
            "REFUND",
            _json(
                {
                    "amount": str(self.approval_amount),
                    "currency": CURRENCY,
                    "order_id": str(order.id),
                    "action_id": str(self.approval_action_id),
                    "order_reference": order.reference,
                }
            ),
            status,
            self.customer_id,
            self.approver if decided else None,
            self.decision_reason if decided else None,
            f"refund:{self.approval_action_id}",
            self.approval_requested_at,
            self.decided_at if decided else None,
            self.executed_at if executed else None,
        )

    # -- templates --

    def build(self, batch: HistoryBatch) -> None:
        self._advance(self._first_delay())
        getattr(self, f"_build_{self.template.lower()}")()
        batch.tickets.append(self._ticket_row())
        batch.messages.extend(self.messages)
        batch.events.extend(self.events)
        if self.approval is not None:
            batch.approvals.append(self.approval)

    def _first_delay(self) -> float:
        if self.detached:
            median = 12 * 3600
        elif self.template.startswith("REFUND"):
            median = 9 * 86400
        elif self.template == "POLICY":
            median = 86400
        else:
            median = 3 * 86400
        delay = self.rng.lognormvariate(math.log(median), 0.8)
        latest = (self.tenant.base_time - self.order.created_at).total_seconds() - 600
        return max(60.0, min(delay, latest))

    def _ticket_row(self) -> tuple[Any, ...]:
        subject_by_template = {
            "ORDER_STATUS": "物流进度咨询",
            "UNKNOWN_ORDER": "物流进度咨询",
            "DEPENDENCY_FAILED": "物流进度咨询",
            "POLICY": "售后政策咨询",
            "NO_POLICY": "售后政策咨询",
            "CANCEL_REFUND": "取消退款申请",
        }
        subject = subject_by_template.get(self.template, "退款申请")
        request_key = f"{THREAD_PREFIX}{self.seq}"
        request_hash = hashlib.sha256(
            f"{self.tenant_id}:{self.customer_id}:{subject}:{self.messages[0][5]}".encode()
        ).hexdigest()
        return (
            self.ticket_id,
            self.tenant_id,
            self.customer_id,
            None if self.detached or self.template == "UNKNOWN_ORDER" else self.order.id,
            f"{THREAD_PREFIX}{self.tenant.config.dataset_id}:{self.tenant_id}:{self.seq}",
            self.status,
            self.result,
            self.category,
            self.priority,
            self.risk,
            subject,
            self.summary,
            _json(self.pending),
            self.triaged_at,
            self.resolved_at,
            self.version,
            self.messages[0][8],
            self.clock,
            None,
            None,
            False,
            self.customer_id,
            request_key,
            request_hash,
        )

    def _build_order_status(self) -> None:
        question = self.rng.choice(_ORDER_STATUS_QUESTIONS).format(ref=self.order.reference)
        run_id, _ = self._start_run(question, first=True)
        self._triage(run_id, "ORDER_STATUS")
        self._tool_order(run_id)
        citations = [self._citation(self._tracking_chunk())]
        self._tool_policy(run_id, 1)
        self._finalize_answered(run_id, self._order_answer(citations), citations)

    def _build_unknown_order(self) -> None:
        wrong = f"{ORDER_REFERENCE_PREFIX}{self.rng.randint(1, 9_999_999):07d}X"
        question = self.rng.choice(_UNKNOWN_REFERENCE_QUESTIONS).format(ref=wrong)
        run_id, _ = self._start_run(question, first=True)
        self._triage(run_id, "ORDER_STATUS")
        self._tool_order(run_id, found=False)
        self._finalize_needs_input(run_id, _UNKNOWN_ORDER_ANSWER, {})
        if self.order.age_days >= 14 and self.rng.random() < 0.7:
            self._wait(2 * 3600, 1.0)
            reply = self.rng.choice(_INFO_REPLY_QUESTIONS).format(ref=self.order.reference)
            run_id, _ = self._start_run(reply, first=False)
            self.pending = {}
            self._triage(run_id, "ORDER_STATUS")
            self._tool_order(run_id)
            citations = [self._citation(self._tracking_chunk())]
            self._tool_policy(run_id, 1)
            self._finalize_answered(run_id, self._order_answer(citations), citations)

    def _build_dependency_failed(self) -> None:
        question = self.rng.choice(_ORDER_STATUS_QUESTIONS).format(ref=self.order.reference)
        run_id, _ = self._start_run(question, first=True)
        self._triage(run_id, "ORDER_STATUS")
        self._tool_order(run_id, timeout=True)
        self._finalize_failed(run_id, "DEPENDENCY_FAILED", "DEPENDENCY_TIMEOUT", _DEPENDENCY_ANSWER)

    def _build_policy(self) -> None:
        chunks = self._random_chunks(self.rng.choice((1, 1, 2)))
        question = chunks[0].title + self.rng.choice(_POLICY_QUESTION_SUFFIXES)
        run_id, _ = self._start_run(question, first=True)
        self._triage(run_id, "POLICY")
        citations = [self._citation(chunk) for chunk in chunks]
        self._tool_policy(run_id, len(citations))
        excerpts = " ".join(chunk.content for chunk in chunks)
        refs = "、".join(f"{chunk.title}（{chunk.chunk_id}）" for chunk in chunks)
        self._finalize_answered(run_id, f"检索到的政策依据为：{excerpts} 参考：{refs}。", citations)

    def _build_no_policy(self) -> None:
        run_id, _ = self._start_run(self.rng.choice(_UNSUPPORTED_POLICY_QUESTIONS), first=True)
        self._triage(run_id, "POLICY")
        self._tool_policy(run_id, 0)
        self._finalize_failed(
            run_id, "INSUFFICIENT_EVIDENCE", "NO_RELEVANT_POLICY", _NO_POLICY_ANSWER
        )

    def _build_cancel_refund(self) -> None:
        question = self.rng.choice(_CANCEL_REFUND_QUESTIONS).format(ref=self.order.reference)
        run_id, _ = self._start_run(question, first=True)
        self._triage(run_id, "OTHER")
        self._tool_order(run_id)
        citations = [self._citation(chunk) for chunk in self._random_chunks(1)]
        self._tool_policy(run_id, 1)
        self._finalize_answered(run_id, self._order_answer(citations), citations)

    def _refund_pending_request(self, amount: Decimal | None) -> dict[str, Any]:
        return {
            "category": "REFUND",
            "priority": "HIGH",
            "order_reference": self.order.reference,
            "full_refund_requested": False,
            "requested_refund_amount": None if amount is None else str(amount),
        }

    def _build_refund_missing_amount(self) -> None:
        question = self.rng.choice(_REFUND_MISSING_QUESTIONS).format(ref=self.order.reference)
        run_id, _ = self._start_run(question, first=True)
        self._triage(run_id, "REFUND")
        self._tool_order(run_id)
        self._finalize_needs_input(
            run_id, _MISSING_AMOUNT_ANSWER, self._refund_pending_request(None)
        )

    def _partial_amount(self) -> Decimal:
        fraction = self.rng.uniform(0.1, 0.6)
        return max(Decimal("0.01"), _money(float(self.order.refundable_amount) * fraction))

    def _build_refund_missing_then_pending(self) -> None:
        self._build_refund_missing_amount()
        self._wait(1800, 1.0)
        amount = self._partial_amount()
        run_id, message_id = self._start_run(f"退 {amount} 元", first=False)
        self.pending = {}
        self._triage(run_id, "REFUND")
        self._tool_order(run_id)
        self._request_approval(run_id, message_id, amount)
        self._write_approval("PENDING", executed=False)

    def _open_refund_run(self, amount: Decimal, full: bool) -> tuple[UUID, UUID]:
        if full:
            question = self.rng.choice(_REFUND_FULL_QUESTIONS).format(ref=self.order.reference)
        else:
            question = self.rng.choice(_REFUND_AMOUNT_QUESTIONS).format(
                ref=self.order.reference, amount=amount
            )
        run_id, message_id = self._start_run(question, first=True)
        self._triage(run_id, "REFUND")
        self._tool_order(run_id)
        return run_id, message_id

    def _build_refund_pending(self) -> None:
        amount = self._partial_amount()
        run_id, message_id = self._open_refund_run(amount, full=False)
        self._request_approval(run_id, message_id, amount)
        self._write_approval("PENDING", executed=False)

    def _build_refund_rejected(self) -> None:
        amount = self._partial_amount()
        run_id, message_id = self._open_refund_run(amount, full=False)
        self._request_approval(run_id, message_id, amount)
        run_id = self._decide_approval("REJECT")
        self._advance(self.rng.uniform(0.2, 0.6))
        self._message(run_id, "AGENT", _REJECTED_ANSWER, [])
        self._event(
            run_id, "REFUND_REJECTED", "SUCCEEDED", "SYSTEM", approval_id=self.approval_id,
            node_name="generate_rejection_response",
            details={"processing_result": "ANSWERED"}, seconds=0.01,
        )  # fmt: skip
        self.status, self.result, self.summary = "RESOLVED", "ANSWERED", _REJECTED_ANSWER
        self.resolved_at = self.clock
        self.version += 1
        self._write_approval("REJECTED", executed=False)

    def _build_refund_executed(self) -> None:
        order = self.order
        amount = order.paid_amount - order.refundable_amount
        full = order.payment_status == "REFUNDED"
        run_id, message_id = self._open_refund_run(amount, full=full)
        self._request_approval(run_id, message_id, amount)
        run_id = self._decide_approval("APPROVE")
        self._advance(self.rng.uniform(0.2, 0.6))
        self.executed_at = self.clock
        answer = f"退款审批已通过，Mock 退款 {amount:.2f} {CURRENCY} 已执行。"
        self._message(run_id, "AGENT", answer, [])
        self._event(
            run_id, "REFUND_EXECUTED", "SUCCEEDED", "SYSTEM", approval_id=self.approval_id,
            node_name="execute_refund_mock", tool_name="execute_refund_mock",
            details={
                "amount": str(amount),
                "remaining_refundable": str(order.refundable_amount),
                "processing_result": "ANSWERED",
            },
            seconds=0.01,
        )  # fmt: skip
        self.status, self.result, self.summary = "RESOLVED", "ANSWERED", answer
        self.resolved_at = self.clock
        self.version += 1
        self._write_approval("EXECUTED", executed=True)
        order.updated_at = self.executed_at
        order.version = 2

    def _build_refund_blocked(self) -> None:
        # The order was fully refunded through another channel before the approver acted.
        order = self.order
        amount = order.paid_amount
        run_id, message_id = self._open_refund_run(amount, full=True)
        self._request_approval(run_id, message_id, amount)
        run_id = self._decide_approval("APPROVE")
        self._advance(self.rng.uniform(0.2, 0.6))
        self._message(run_id, "AGENT", _BLOCKED_ANSWER, [])
        self._event(
            run_id, "REFUND_EXECUTION_BLOCKED", "BLOCKED", "SYSTEM", approval_id=self.approval_id,
            node_name="execute_refund_mock", tool_name="execute_refund_mock",
            details={"processing_result": "PROCESSING_FAILED"}, seconds=0.01,
        )  # fmt: skip
        self.status, self.result, self.summary = "FAILED", "PROCESSING_FAILED", _BLOCKED_ANSWER
        self.version += 1
        self._write_approval("CANCELLED", executed=False)
        assert self.decided_at is not None
        order.updated_at = min(self.decided_at, self.tenant.base_time)
        order.version = 2

    def _build_refund_over_limit(self) -> None:
        amount = _money(float(self.order.refundable_amount) * self.rng.uniform(1.2, 3.0) + 1)
        run_id, _ = self._open_refund_run(amount, full=False)
        answer = _ORDER_STATUS_ANSWER_LIMIT.format(limit=f"{self.order.refundable_amount:.2f}")
        self._finalize_answered(run_id, answer, [])

    def _build_refund_zero_balance(self) -> None:
        amount = _money(self.rng.uniform(10, 300))
        run_id, _ = self._open_refund_run(amount, full=False)
        self._finalize_answered(run_id, _ZERO_BALANCE_ANSWER, [])


def generate_history(config: HistoryConfig, batch_size: int = 20_000) -> Iterator[HistoryBatch]:
    """Yield batches of rows in foreign-key order; memory stays bounded by batch_size."""
    corpus = load_policy_corpus(config.policy_manifest_path)
    calendar = _Calendar(config)
    batch = HistoryBatch()
    for profile, count in zip(config.tenants, config.tenant_order_counts(), strict=True):
        tenant = _TenantGenerator(config, profile, count, calendar, corpus.chunks)
        for seq in range(1, count + 1):
            tenant.emit_order(seq, batch)
            if len(batch.orders) >= batch_size:
                yield batch
                batch = HistoryBatch()
    if batch.orders:
        yield batch


def summarize_history(config: HistoryConfig, batch_size: int = 20_000) -> dict[str, Any]:
    """Generate without loading, returning counts and distributions for a dry run."""
    totals = {"orders": 0, "tickets": 0, "ticket_messages": 0, "approvals": 0, "audit_events": 0}
    payment: dict[str, int] = {}
    ticket_status: dict[str, int] = {}
    approval_status: dict[str, int] = {}
    tenants: dict[str, int] = {}
    for batch in generate_history(config, batch_size):
        for key, value in batch.row_counts().items():
            totals[key] += value
        for row in batch.orders:
            payment[row[4]] = payment.get(row[4], 0) + 1
            tenants[row[1]] = tenants.get(row[1], 0) + 1
        for row in batch.tickets:
            ticket_status[row[5]] = ticket_status.get(row[5], 0) + 1
        for row in batch.approvals:
            approval_status[row[7]] = approval_status.get(row[7], 0) + 1
    return {
        "dataset_id": config.dataset_id,
        "fingerprint": config.fingerprint(),
        "row_counts": totals,
        "orders_by_tenant": tenants,
        "payment_distribution": payment,
        "ticket_status_distribution": ticket_status,
        "approval_status_distribution": approval_status,
    }
