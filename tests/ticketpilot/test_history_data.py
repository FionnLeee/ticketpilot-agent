from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from ticketpilot.history_data import (
    EVENT_COLUMNS,
    MESSAGE_COLUMNS,
    ORDER_COLUMNS,
    TICKET_COLUMNS,
    generate_history,
    load_history_config,
    summarize_history,
    with_order_count,
)
from ticketpilot.history_loader import TABLE_COLUMNS, format_copy_text
from ticketpilot.history_quality import ANALYTICS_VIEWS, QUALITY_CHECKS


def test_history_manifest_is_reproducible_and_scales_into_an_isolated_namespace():
    original = load_history_config()
    scaled = with_order_count(original, 1_000)

    assert original.order_count == 1_000_000
    assert scaled.dataset_id == "ticketpilot-history-v1-1000"
    assert scaled.tenant_prefix == "tenant-hist-1000-"
    assert sum(scaled.tenant_order_counts()) == 1_000
    assert len(scaled.tenant_ids()) == 12
    assert len(scaled.fingerprint()) == 64
    assert scaled.fingerprint() == with_order_count(original, 1_000).fingerprint()
    assert scaled.contains_real_personal_data is False


def test_streaming_history_preserves_relations_and_run_event_order():
    config = with_order_count(load_history_config(), 1_000)
    batches = list(generate_history(config, batch_size=200))

    assert len(batches) == 5
    assert all(len(batch.orders) <= 200 for batch in batches)
    orders = [row for batch in batches for row in batch.orders]
    tickets = [row for batch in batches for row in batch.tickets]
    messages = [row for batch in batches for row in batch.messages]
    approvals = [row for batch in batches for row in batch.approvals]
    events = [row for batch in batches for row in batch.events]
    assert len(orders) == 1_000
    assert tickets and messages and approvals and events

    order_by_key = {(row[1], row[0]): row for row in orders}
    ticket_by_key = {(row[1], row[0]): row for row in tickets}
    approval_keys = {(row[1], row[0]) for row in approvals}
    assert len({row[4] for row in tickets}) == len(tickets)
    for ticket in tickets:
        if ticket[3] is not None:
            order = order_by_key[(ticket[1], ticket[3])]
            assert order[3] == ticket[2]
    assert all((row[1], row[2]) in ticket_by_key for row in messages)
    assert all((row[1], row[2]) in ticket_by_key for row in approvals)
    assert all(row[2] is None or (row[1], row[2]) in ticket_by_key for row in events)
    assert all(row[4] is None or (row[1], row[4]) in approval_keys for row in events)

    earliest_by_run = {}
    for event in events:
        run_key = (event[1], event[3])
        if event[3] is None:
            continue
        previous = earliest_by_run.get(run_key)
        if previous is None or (event[12], str(event[0])) < (previous[12], str(previous[0])):
            earliest_by_run[run_key] = event
    assert earliest_by_run
    assert {row[7] for row in earliest_by_run.values()} == {"RUN_STARTED"}


def test_dry_run_summary_matches_streamed_counts():
    config = with_order_count(load_history_config(), 1_000)
    first = summarize_history(config, batch_size=200)
    second = summarize_history(config, batch_size=333)

    assert first == second
    assert first["row_counts"]["orders"] == 1_000
    assert sum(first["orders_by_tenant"].values()) == 1_000


def test_copy_formatter_and_column_maps_cover_supported_values():
    rendered = format_copy_text(
        [
            (
                UUID("00000000-0000-0000-0000-000000000001"),
                "tab\tline\nslash\\",
                Decimal("12.30"),
                datetime(2026, 9, 15, tzinfo=UTC),
                True,
                None,
            )
        ]
    )

    assert "tab\\tline\\nslash\\\\" in rendered
    assert rendered.endswith("t\t\\N\n")
    assert dict(TABLE_COLUMNS)["orders"] == ORDER_COLUMNS
    assert dict(TABLE_COLUMNS)["tickets"] == TICKET_COLUMNS
    assert dict(TABLE_COLUMNS)["ticket_messages"] == MESSAGE_COLUMNS
    assert dict(TABLE_COLUMNS)["audit_events"] == EVENT_COLUMNS


def test_quality_and_analytics_contracts_are_named_and_nontrivial():
    quality_names = [check.name for check in QUALITY_CHECKS]
    view_names = [name for name, _ in ANALYTICS_VIEWS]

    assert len(quality_names) >= 20
    assert len(quality_names) == len(set(quality_names))
    assert set(view_names) == {
        "data_origin_summary",
        "tenant_overview",
        "ticket_status_by_tenant",
        "refund_approval_funnel",
        "daily_ticket_volume_last_14_days",
        "open_backlog_aging",
    }
