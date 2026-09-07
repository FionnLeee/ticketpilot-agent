from typing import Protocol

from ticketpilot.db import BusinessPool
from ticketpilot.domain import PrincipalRole
from ticketpilot.errors import Forbidden
from ticketpilot.privacy import mask_tracking_number
from ticketpilot.schemas import OrderSummary, RequestPrincipal


class OrderReader(Protocol):
    async def get_by_reference(
        self, principal: RequestPrincipal, order_reference: str
    ) -> OrderSummary | None: ...


class PostgresOrderRepository:
    def __init__(self, pool: BusinessPool) -> None:
        self.pool = pool

    async def get_by_reference(
        self, principal: RequestPrincipal, order_reference: str
    ) -> OrderSummary | None:
        async with self.pool.connection() as connection:
            if principal.role is PrincipalRole.CUSTOMER:
                cursor = await connection.execute(
                    """
                    SELECT
                        order_reference, payment_status, fulfillment_status,
                        paid_amount, refundable_amount, currency, carrier,
                        tracking_number, estimated_delivery_at
                    FROM ticketpilot.orders
                    WHERE tenant_id = %s AND order_reference = %s AND customer_id = %s
                    """,
                    (principal.tenant_id, order_reference, principal.actor_id),
                )
            elif principal.role in {PrincipalRole.STAFF, PrincipalRole.ADMIN}:
                cursor = await connection.execute(
                    """
                    SELECT
                        order_reference, payment_status, fulfillment_status,
                        paid_amount, refundable_amount, currency, carrier,
                        tracking_number, estimated_delivery_at
                    FROM ticketpilot.orders
                    WHERE tenant_id = %s AND order_reference = %s
                    """,
                    (principal.tenant_id, order_reference),
                )
            else:
                raise Forbidden("This principal cannot query orders")

            row = await cursor.fetchone()
        if row is None:
            return None
        return OrderSummary(
            order_reference=row["order_reference"],
            payment_status=row["payment_status"],
            fulfillment_status=row["fulfillment_status"],
            paid_amount=row["paid_amount"],
            refundable_amount=row["refundable_amount"],
            currency=row["currency"],
            carrier=row["carrier"],
            tracking_number_masked=mask_tracking_number(row["tracking_number"]),
            estimated_delivery_at=row["estimated_delivery_at"],
        )
