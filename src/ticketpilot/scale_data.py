import random
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid5

from ticketpilot.db import BusinessPool
from ticketpilot.seed import SEED_NAMESPACE, SeedManifest, SeedOrder, generate_seed_orders

DATASET_ID = "ticketpilot-scale-v1"


def generate_scale_orders(seed: int = 20260915) -> list[SeedOrder]:
    manifest = SeedManifest(
        DATASET_ID, seed, datetime(2026, 9, 15, tzinfo=UTC), 10, 100, 1000, 10000
    )
    rng = random.Random(seed)
    orders = []
    for index, order in enumerate(generate_seed_orders(manifest)):
        tenant = order.tenant_id.replace("tenant-demo", "tenant-scale")
        reference = order.order_reference.replace("TP-", "O-SCALE-")
        paid = Decimal(rng.randint(100, 999900)) / 100 if order.paid_amount else Decimal("0")
        refundable = (
            paid
            if order.refundable_amount == order.paid_amount
            else min(paid, Decimal(rng.randint(1, max(1, int(paid * 100)))) / 100)
            if order.refundable_amount
            else Decimal("0")
        )
        orders.append(
            replace(
                order,
                id=uuid5(SEED_NAMESPACE, f"{seed}:{tenant}:{reference}"),
                tenant_id=tenant,
                order_reference=reference,
                customer_id=f"customer-scale-{rng.randint(1, 100):03d}",
                paid_amount=paid,
                refundable_amount=refundable,
                created_at=manifest.base_time - timedelta(days=rng.randint(1, 365)),
                estimated_delivery_at=(
                    manifest.base_time + timedelta(days=(index % 15) - 7)
                    if order.estimated_delivery_at
                    else None
                ),
            )
        )
    return orders


def describe_orders(orders: list[SeedOrder]) -> dict:
    return {
        "dataset_id": DATASET_ID,
        "synthetic": True,
        "order_count": len(orders),
        "tenant_count": len({o.tenant_id for o in orders}),
        "tenant_customer_count": len({(o.tenant_id, o.customer_id) for o in orders}),
        "payment_distribution": dict(Counter(o.payment_status.value for o in orders)),
        "fulfillment_distribution": dict(Counter(o.fulfillment_status.value for o in orders)),
        "scenario_families": 10,
        "currency": "CNY",
        "created_at_min": min(o.created_at for o in orders).isoformat(),
        "created_at_max": max(o.created_at for o in orders).isoformat(),
        "generator_note": "10 state templates; randomized amounts, customers and dates, not 10000 independent semantic cases.",
    }


async def insert_scale_orders(pool: BusinessPool, orders: list[SeedOrder]) -> None:
    async with pool.connection() as connection, connection.transaction():
        async with connection.cursor() as cursor:
            await cursor.executemany(
                """
                INSERT INTO ticketpilot.orders (
                    id, tenant_id, order_reference, customer_id, payment_status,
                    fulfillment_status, paid_amount, refundable_amount, currency,
                    carrier, tracking_number, estimated_delivery_at, created_at, updated_at
                ) VALUES (
                    %(id)s, %(tenant_id)s, %(order_reference)s, %(customer_id)s,
                    %(payment_status)s, %(fulfillment_status)s, %(paid_amount)s,
                    %(refundable_amount)s, %(currency)s, %(carrier)s, %(tracking_number)s,
                    %(estimated_delivery_at)s, %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (tenant_id, order_reference) DO NOTHING
                """,
                [asdict(order) for order in orders],
            )
