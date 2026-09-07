import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ticketpilot.db import BusinessPool
from ticketpilot.domain import FulfillmentStatus, OrderPaymentStatus
from ticketpilot.paths import find_ancestor_path

DEFAULT_MANIFEST_PATH = find_ancestor_path(
    Path(__file__), "data", "ticketpilot", "seed_manifest.json"
)
SEED_NAMESPACE = UUID("b2845781-e38f-4a17-95b2-3d22dcf57e09")


@dataclass(frozen=True)
class SeedManifest:
    dataset_id: str
    generator_seed: int
    base_time: datetime
    tenant_count: int
    customers_per_tenant: int
    orders_per_tenant: int
    expected_order_count: int


@dataclass(frozen=True)
class SeedScenario:
    payment_status: OrderPaymentStatus
    fulfillment_status: FulfillmentStatus
    paid_amount: Decimal
    refundable_amount: Decimal
    carrier: str | None = None
    eta_days: int | None = None


@dataclass(frozen=True)
class SeedOrder:
    id: UUID
    tenant_id: str
    order_reference: str
    customer_id: str
    payment_status: OrderPaymentStatus
    fulfillment_status: FulfillmentStatus
    paid_amount: Decimal
    refundable_amount: Decimal
    currency: str
    carrier: str | None
    tracking_number: str | None
    estimated_delivery_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SeedResult:
    dataset_id: str
    order_count: int
    tenant_count: int
    orders_per_tenant: int


SCENARIOS = (
    SeedScenario(
        OrderPaymentStatus.PENDING, FulfillmentStatus.PENDING, Decimal("49.90"), Decimal("0.00")
    ),
    SeedScenario(
        OrderPaymentStatus.PAID, FulfillmentStatus.PROCESSING, Decimal("199.00"), Decimal("199.00")
    ),
    SeedScenario(
        OrderPaymentStatus.PAID,
        FulfillmentStatus.SHIPPED,
        Decimal("399.00"),
        Decimal("399.00"),
        "SF Express",
        2,
    ),
    SeedScenario(
        OrderPaymentStatus.PAID,
        FulfillmentStatus.SHIPPED,
        Decimal("899.00"),
        Decimal("899.00"),
        "YTO Express",
        -1,
    ),
    SeedScenario(
        OrderPaymentStatus.PAID,
        FulfillmentStatus.DELIVERED,
        Decimal("129.00"),
        Decimal("129.00"),
        "ZTO Express",
    ),
    SeedScenario(
        OrderPaymentStatus.PARTIALLY_REFUNDED,
        FulfillmentStatus.DELIVERED,
        Decimal("599.00"),
        Decimal("200.00"),
        "SF Express",
    ),
    SeedScenario(
        OrderPaymentStatus.REFUNDED,
        FulfillmentStatus.DELIVERED,
        Decimal("259.00"),
        Decimal("0.00"),
        "JD Logistics",
    ),
    SeedScenario(
        OrderPaymentStatus.CANCELLED, FulfillmentStatus.CANCELLED, Decimal("79.00"), Decimal("0.00")
    ),
    SeedScenario(
        OrderPaymentStatus.PAID,
        FulfillmentStatus.SHIPPED,
        Decimal("9999.00"),
        Decimal("9999.00"),
        "JD Logistics",
        1,
    ),
    SeedScenario(
        OrderPaymentStatus.PAID,
        FulfillmentStatus.DELIVERED,
        Decimal("0.00"),
        Decimal("0.00"),
        "Cainiao",
    ),
)


def load_seed_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> SeedManifest:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    manifest = SeedManifest(
        dataset_id=payload["dataset_id"],
        generator_seed=payload["generator_seed"],
        base_time=datetime.fromisoformat(payload["base_time"]),
        tenant_count=payload["tenant_count"],
        customers_per_tenant=payload["customers_per_tenant"],
        orders_per_tenant=payload["orders_per_tenant"],
        expected_order_count=payload["expected_order_count"],
    )
    if manifest.base_time.tzinfo is None:
        raise ValueError("Seed manifest base_time must include a timezone")
    if manifest.customers_per_tenant < 1 or manifest.orders_per_tenant < 1:
        raise ValueError("Seed manifest counts must be positive")
    if manifest.expected_order_count != manifest.tenant_count * manifest.orders_per_tenant:
        raise ValueError("Seed manifest expected_order_count is inconsistent")
    return manifest


def generate_seed_orders(manifest: SeedManifest | None = None) -> list[SeedOrder]:
    manifest = manifest or load_seed_manifest()
    orders: list[SeedOrder] = []
    for tenant_index in range(1, manifest.tenant_count + 1):
        tenant_id = f"tenant-demo-{tenant_index:02d}"
        for order_index in range(1, manifest.orders_per_tenant + 1):
            scenario = SCENARIOS[(order_index - 1) % len(SCENARIOS)]
            cycle = (order_index - 1) // len(SCENARIOS)
            paid_amount = scenario.paid_amount
            refundable_amount = scenario.refundable_amount
            if paid_amount > 0:
                increment = Decimal(cycle * 17)
                paid_amount += increment
                if refundable_amount > 0:
                    refundable_amount = min(refundable_amount + increment, paid_amount)

            order_reference = f"TP-{order_index:04d}"
            customer_id = (
                f"customer-demo-{((order_index - 1) % manifest.customers_per_tenant) + 1:02d}"
            )
            has_tracking = scenario.fulfillment_status in {
                FulfillmentStatus.SHIPPED,
                FulfillmentStatus.DELIVERED,
            }
            tracking_number = f"TR{tenant_index:02d}{order_index:08d}" if has_tracking else None
            estimated_delivery_at = (
                manifest.base_time + timedelta(days=scenario.eta_days)
                if scenario.eta_days is not None
                else None
            )
            orders.append(
                SeedOrder(
                    id=uuid5(
                        SEED_NAMESPACE, f"{manifest.generator_seed}:{tenant_id}:{order_reference}"
                    ),
                    tenant_id=tenant_id,
                    order_reference=order_reference,
                    customer_id=customer_id,
                    payment_status=scenario.payment_status,
                    fulfillment_status=scenario.fulfillment_status,
                    paid_amount=paid_amount,
                    refundable_amount=refundable_amount,
                    currency="CNY",
                    carrier=scenario.carrier,
                    tracking_number=tracking_number,
                    estimated_delivery_at=estimated_delivery_at,
                    created_at=manifest.base_time - timedelta(days=30 - order_index % 30),
                    updated_at=manifest.base_time,
                )
            )

    if len(orders) != manifest.expected_order_count:
        raise ValueError("Generated order count does not match the seed manifest")
    return orders


async def seed_orders(pool: BusinessPool) -> SeedResult:
    manifest = load_seed_manifest()
    orders = generate_seed_orders(manifest)
    async with pool.connection() as connection, connection.transaction():
        for order in orders:
            await connection.execute(
                """
                INSERT INTO ticketpilot.orders (
                    id, tenant_id, order_reference, customer_id,
                    payment_status, fulfillment_status, paid_amount, currency,
                    refundable_amount, carrier, tracking_number,
                    estimated_delivery_at, version, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 1, %s, %s
                )
                ON CONFLICT (tenant_id, order_reference) DO UPDATE SET
                    id = EXCLUDED.id,
                    customer_id = EXCLUDED.customer_id,
                    payment_status = EXCLUDED.payment_status,
                    fulfillment_status = EXCLUDED.fulfillment_status,
                    paid_amount = EXCLUDED.paid_amount,
                    currency = EXCLUDED.currency,
                    refundable_amount = EXCLUDED.refundable_amount,
                    carrier = EXCLUDED.carrier,
                    tracking_number = EXCLUDED.tracking_number,
                    estimated_delivery_at = EXCLUDED.estimated_delivery_at,
                    version = EXCLUDED.version,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    order.id,
                    order.tenant_id,
                    order.order_reference,
                    order.customer_id,
                    order.payment_status.value,
                    order.fulfillment_status.value,
                    order.paid_amount,
                    order.currency,
                    order.refundable_amount,
                    order.carrier,
                    order.tracking_number,
                    order.estimated_delivery_at,
                    order.created_at,
                    order.updated_at,
                ),
            )
    return SeedResult(
        dataset_id=manifest.dataset_id,
        order_count=len(orders),
        tenant_count=manifest.tenant_count,
        orders_per_tenant=manifest.orders_per_tenant,
    )
