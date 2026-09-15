"""Data-quality checks and analytics snapshots for the synthetic history dataset."""

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, LiteralString, cast

from ticketpilot.db import BusinessPool
from ticketpilot.history_data import THREAD_PREFIX, HistoryConfig

_ALLOWED_ORDER_STATES = (
    "('PENDING','PENDING'),('PAID','PROCESSING'),('PAID','SHIPPED'),('PAID','DELIVERED'),"
    "('PARTIALLY_REFUNDED','DELIVERED'),('REFUNDED','DELIVERED'),('REFUNDED','CANCELLED'),"
    "('CANCELLED','CANCELLED')"
)


@dataclass(frozen=True)
class QualityCheck:
    name: str
    description: str
    sql: str


# Every query returns one row with a single `violations` column; the namespace is bound as
# %(tenants)s and the snapshot window as %(window_start)s / %(base_time)s.
QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck(
        "orders_refundable_within_paid",
        "0 <= refundable_amount <= paid_amount",
        """
        SELECT count(*) AS violations FROM ticketpilot.orders
        WHERE tenant_id = ANY(%(tenants)s)
          AND (refundable_amount < 0 OR refundable_amount > paid_amount)
        """,
    ),
    QualityCheck(
        "orders_status_pairs_allowed",
        "payment/fulfillment status pair is one of the modelled combinations",
        f"""
        SELECT count(*) AS violations FROM ticketpilot.orders
        WHERE tenant_id = ANY(%(tenants)s)
          AND (payment_status, fulfillment_status) NOT IN ({_ALLOWED_ORDER_STATES})
        """,
    ),
    QualityCheck(
        "orders_refundable_matches_payment_status",
        "PAID keeps the full amount refundable; refunded/cancelled/pending keep none",
        """
        SELECT count(*) AS violations FROM ticketpilot.orders
        WHERE tenant_id = ANY(%(tenants)s)
          AND NOT (
            (payment_status = 'PAID' AND refundable_amount = paid_amount)
            OR (payment_status = 'PARTIALLY_REFUNDED'
                AND refundable_amount > 0 AND refundable_amount < paid_amount)
            OR (payment_status IN ('REFUNDED', 'CANCELLED', 'PENDING') AND refundable_amount = 0)
          )
        """,
    ),
    QualityCheck(
        "orders_shipping_fields_consistent",
        "carrier, tracking number and ETA exist exactly when shipped or delivered",
        """
        SELECT count(*) AS violations FROM ticketpilot.orders
        WHERE tenant_id = ANY(%(tenants)s)
          AND (fulfillment_status IN ('SHIPPED', 'DELIVERED'))
              <> (carrier IS NOT NULL AND tracking_number IS NOT NULL
                  AND estimated_delivery_at IS NOT NULL)
        """,
    ),
    QualityCheck(
        "orders_within_snapshot_window",
        "created_at inside the history window and updated_at never before created_at",
        """
        SELECT count(*) AS violations FROM ticketpilot.orders
        WHERE tenant_id = ANY(%(tenants)s)
          AND (created_at < %(window_start)s OR created_at > %(base_time)s
               OR updated_at < created_at OR updated_at > %(base_time)s)
        """,
    ),
    QualityCheck(
        "tickets_customer_owns_order",
        "linked order belongs to the same tenant and customer as the ticket",
        """
        SELECT count(*) AS violations
        FROM ticketpilot.tickets AS t
        JOIN ticketpilot.orders AS o ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
        WHERE t.tenant_id = ANY(%(tenants)s) AND o.customer_id <> t.customer_id
        """,
    ),
    QualityCheck(
        "tickets_created_after_order",
        "a ticket about an order is opened after the order exists",
        """
        SELECT count(*) AS violations
        FROM ticketpilot.tickets AS t
        JOIN ticketpilot.orders AS o ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
        WHERE t.tenant_id = ANY(%(tenants)s) AND t.created_at < o.created_at
        """,
    ),
    QualityCheck(
        "tickets_timestamps_ordered",
        "created <= triaged <= resolved <= updated, resolved_at only when RESOLVED",
        """
        SELECT count(*) AS violations FROM ticketpilot.tickets
        WHERE tenant_id = ANY(%(tenants)s)
          AND (updated_at < created_at
               OR triaged_at < created_at
               OR resolved_at < coalesce(triaged_at, created_at)
               OR updated_at < resolved_at
               OR (resolved_at IS NOT NULL) <> (status = 'RESOLVED'))
        """,
    ),
    QualityCheck(
        "tickets_within_snapshot_time",
        "no ticket activity is dated after the snapshot time",
        """
        SELECT count(*) AS violations FROM ticketpilot.tickets
        WHERE tenant_id = ANY(%(tenants)s)
          AND thread_id LIKE %(thread_pattern)s AND updated_at > %(base_time)s
        """,
    ),
    QualityCheck(
        "tickets_waiting_approval_have_one_pending",
        "WAITING_APPROVAL <=> exactly one PENDING approval",
        """
        WITH pending AS (
            SELECT tenant_id, ticket_id, count(*) AS n
            FROM ticketpilot.approvals
            WHERE tenant_id = ANY(%(tenants)s) AND status = 'PENDING'
            GROUP BY tenant_id, ticket_id
        )
        SELECT count(*) AS violations
        FROM ticketpilot.tickets AS t
        LEFT JOIN pending AS p ON p.tenant_id = t.tenant_id AND p.ticket_id = t.id
        WHERE t.tenant_id = ANY(%(tenants)s)
          AND ((t.status = 'WAITING_APPROVAL') <> (coalesce(p.n, 0) = 1))
        """,
    ),
    QualityCheck(
        "approvals_amount_positive_within_paid",
        "0 < requested amount <= order paid_amount",
        """
        SELECT count(*) AS violations
        FROM ticketpilot.approvals AS a
        JOIN ticketpilot.tickets AS t ON t.tenant_id = a.tenant_id AND t.id = a.ticket_id
        JOIN ticketpilot.orders AS o ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
        WHERE a.tenant_id = ANY(%(tenants)s)
          AND ((a.action_payload ->> 'amount')::numeric <= 0
               OR (a.action_payload ->> 'amount')::numeric > o.paid_amount
               OR a.action_payload ->> 'order_id' <> o.id::text)
        """,
    ),
    QualityCheck(
        "approvals_executed_match_order_balance",
        "an executed refund equals paid - refundable and the order shows the refund",
        """
        SELECT count(*) AS violations
        FROM ticketpilot.approvals AS a
        JOIN ticketpilot.tickets AS t ON t.tenant_id = a.tenant_id AND t.id = a.ticket_id
        JOIN ticketpilot.orders AS o ON o.tenant_id = t.tenant_id AND o.id = t.order_pk
        WHERE a.tenant_id = ANY(%(tenants)s) AND a.status = 'EXECUTED'
          AND ((a.action_payload ->> 'amount')::numeric <> o.paid_amount - o.refundable_amount
               OR o.payment_status NOT IN ('PARTIALLY_REFUNDED', 'REFUNDED'))
        """,
    ),
    QualityCheck(
        "approvals_at_most_one_executed_per_order",
        "no order carries two executed refunds",
        """
        SELECT count(*) AS violations FROM (
            SELECT t.tenant_id, t.order_pk
            FROM ticketpilot.approvals AS a
            JOIN ticketpilot.tickets AS t ON t.tenant_id = a.tenant_id AND t.id = a.ticket_id
            WHERE a.tenant_id = ANY(%(tenants)s) AND a.status = 'EXECUTED'
            GROUP BY t.tenant_id, t.order_pk
            HAVING count(*) > 1
        ) AS duplicated
        """,
    ),
    QualityCheck(
        "approvals_timestamps_ordered",
        "requested <= decided <= executed, all before the snapshot time",
        """
        SELECT count(*) AS violations FROM ticketpilot.approvals
        WHERE tenant_id = ANY(%(tenants)s)
          AND (decided_at < requested_at OR executed_at < decided_at
               OR coalesce(executed_at, decided_at, requested_at) > %(base_time)s)
        """,
    ),
    QualityCheck(
        "messages_first_is_customer",
        "every ticket conversation starts with the customer",
        """
        SELECT count(*) AS violations FROM (
            SELECT DISTINCT ON (tenant_id, ticket_id) role
            FROM ticketpilot.ticket_messages
            WHERE tenant_id = ANY(%(tenants)s)
            ORDER BY tenant_id, ticket_id, created_at, id
        ) AS first_messages
        WHERE role <> 'CUSTOMER'
        """,
    ),
    QualityCheck(
        "messages_within_ticket_window",
        "messages are dated between ticket creation and last update",
        """
        SELECT count(*) AS violations
        FROM ticketpilot.ticket_messages AS m
        JOIN ticketpilot.tickets AS t ON t.tenant_id = m.tenant_id AND t.id = m.ticket_id
        WHERE m.tenant_id = ANY(%(tenants)s)
          AND (m.created_at < t.created_at OR m.created_at > t.updated_at)
        """,
    ),
    QualityCheck(
        "events_ticket_created_exactly_once",
        "each ticket has exactly one TICKET_CREATED event",
        """
        SELECT count(*) AS violations
        FROM ticketpilot.tickets AS t
        LEFT JOIN LATERAL (
            SELECT count(*) AS n FROM ticketpilot.audit_events AS e
            WHERE e.tenant_id = t.tenant_id AND e.ticket_id = t.id
              AND e.event_type = 'TICKET_CREATED'
        ) AS c ON true
        WHERE t.tenant_id = ANY(%(tenants)s) AND c.n <> 1
        """,
    ),
    QualityCheck(
        "events_run_starts_with_run_started",
        "the earliest event of every run is RUN_STARTED",
        """
        SELECT count(*) AS violations FROM (
            SELECT DISTINCT ON (tenant_id, run_id) event_type
            FROM ticketpilot.audit_events
            WHERE tenant_id = ANY(%(tenants)s) AND run_id IS NOT NULL
            ORDER BY tenant_id, run_id, occurred_at, id
        ) AS first_events
        WHERE event_type <> 'RUN_STARTED'
        """,
    ),
    QualityCheck(
        "events_approval_belongs_to_ticket",
        "approval-scoped events reference an approval of the same ticket",
        """
        SELECT count(*) AS violations
        FROM ticketpilot.audit_events AS e
        JOIN ticketpilot.approvals AS a ON a.tenant_id = e.tenant_id AND a.id = e.approval_id
        WHERE e.tenant_id = ANY(%(tenants)s) AND a.ticket_id <> e.ticket_id
        """,
    ),
    QualityCheck(
        "events_within_ticket_window",
        "audit events are dated between ticket creation and last update",
        """
        SELECT count(*) AS violations
        FROM ticketpilot.audit_events AS e
        JOIN ticketpilot.tickets AS t ON t.tenant_id = e.tenant_id AND t.id = e.ticket_id
        WHERE e.tenant_id = ANY(%(tenants)s)
          AND (e.occurred_at < t.created_at OR e.occurred_at > t.updated_at)
        """,
    ),
    QualityCheck(
        "synthetic_rows_are_marked",
        "synthetic tickets carry the thread prefix and the matching request key",
        """
        SELECT count(*) AS violations FROM ticketpilot.tickets
        WHERE tenant_id = ANY(%(tenants)s)
          AND (thread_id LIKE %(thread_pattern)s)
              <> (create_idempotency_key LIKE %(thread_pattern)s)
        """,
    ),
)

ANALYTICS_VIEWS: tuple[tuple[str, str], ...] = (
    ("data_origin_summary", "SELECT * FROM ticketpilot.data_origin_summary ORDER BY data_origin"),
    (
        "tenant_overview",
        "SELECT * FROM ticketpilot.tenant_overview WHERE tenant_id = ANY(%(tenants)s) "
        "ORDER BY order_count DESC",
    ),
    (
        "ticket_status_by_tenant",
        "SELECT status, processing_result, category, sum(ticket_count) AS ticket_count "
        "FROM ticketpilot.ticket_status_by_tenant WHERE tenant_id = ANY(%(tenants)s) "
        "GROUP BY 1, 2, 3 ORDER BY 4 DESC",
    ),
    (
        "refund_approval_funnel",
        "SELECT status, sum(approval_count) AS approval_count, "
        "round(sum(requested_amount), 2) AS requested_amount, "
        "round((sum(decision_hours_sum) / nullif(sum(decided_count), 0))::numeric, 2) "
        "AS avg_decision_hours "
        "FROM ticketpilot.refund_approval_funnel WHERE tenant_id = ANY(%(tenants)s) "
        "GROUP BY 1 ORDER BY 2 DESC",
    ),
    (
        "daily_ticket_volume_last_14_days",
        "SELECT day, sum(created_count) AS created_count, sum(refund_count) AS refund_count, "
        "sum(resolved_count) AS resolved_count "
        "FROM ticketpilot.daily_ticket_volume WHERE tenant_id = ANY(%(tenants)s) "
        "AND day >= (%(base_time)s::timestamptz - interval '14 days')::date "
        "GROUP BY 1 ORDER BY 1",
    ),
    (
        "open_backlog_aging",
        "SELECT status, age_bucket, sum(ticket_count) AS ticket_count "
        "FROM ticketpilot.open_backlog_aging WHERE tenant_id = ANY(%(tenants)s) "
        "GROUP BY 1, 2 ORDER BY 1, 2",
    ),
)


def _parameters(config: HistoryConfig) -> dict[str, Any]:
    return {
        "tenants": config.tenant_ids(),
        "base_time": config.base_time,
        "window_start": config.base_time - timedelta(days=config.history_days),
        "thread_pattern": f"{THREAD_PREFIX}%",
    }


async def run_quality_checks(pool: BusinessPool, config: HistoryConfig) -> list[dict[str, Any]]:
    parameters = _parameters(config)
    results = []
    async with pool.connection() as connection:
        for check in QUALITY_CHECKS:
            cursor = await connection.execute(cast(LiteralString, check.sql), parameters)
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError(f"Quality check returned no row: {check.name}")
            violations = row["violations"]
            results.append(
                {
                    "name": check.name,
                    "description": check.description,
                    "violations": violations,
                    "passed": violations == 0,
                }
            )
    return results


async def snapshot_views(pool: BusinessPool, config: HistoryConfig) -> dict[str, list[dict]]:
    measured = await snapshot_views_with_timings(pool, config)
    return {name: result["rows"] for name, result in measured.items()}


async def snapshot_views_with_timings(
    pool: BusinessPool, config: HistoryConfig
) -> dict[str, dict[str, Any]]:
    parameters = _parameters(config)
    snapshots: dict[str, dict[str, Any]] = {}
    async with pool.connection() as connection:
        for name, sql in ANALYTICS_VIEWS:
            started = time.perf_counter()
            cursor = await connection.execute(cast(LiteralString, sql), parameters)
            rows = [
                {key: _plain(value) for key, value in row.items()}
                for row in await cursor.fetchall()
            ]
            snapshots[name] = {
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "row_count": len(rows),
                "rows": rows,
            }
    return snapshots


def _plain(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None or isinstance(value, int | str | bool):
        return value
    return str(value)
