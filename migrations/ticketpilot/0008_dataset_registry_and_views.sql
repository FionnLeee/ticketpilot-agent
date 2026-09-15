CREATE TABLE ticketpilot.synthetic_datasets (
    dataset_id text PRIMARY KEY,
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    row_counts jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(row_counts) = 'object'),
    loaded_at timestamptz NOT NULL DEFAULT now()
);

-- Synthetic history tickets carry a 'synthetic-history:' thread_id prefix; live runs use a UUID.
CREATE VIEW ticketpilot.ticket_origins AS
SELECT
    tenant_id,
    id AS ticket_id,
    customer_id,
    order_pk,
    status,
    processing_result,
    category,
    created_at,
    resolved_at,
    updated_at,
    CASE
        WHEN thread_id LIKE 'synthetic-history:%' THEN 'SYNTHETIC_HISTORY'
        ELSE 'LIVE_RUN'
    END AS data_origin
FROM ticketpilot.tickets;

CREATE VIEW ticketpilot.ticket_status_by_tenant AS
SELECT
    tenant_id,
    data_origin,
    status,
    processing_result,
    category,
    count(*) AS ticket_count
FROM ticketpilot.ticket_origins
GROUP BY tenant_id, data_origin, status, processing_result, category;

CREATE VIEW ticketpilot.refund_approval_funnel AS
SELECT
    a.tenant_id,
    t.data_origin,
    a.status,
    count(*) AS approval_count,
    sum((a.action_payload ->> 'amount')::numeric) AS requested_amount,
    count(a.decided_at) AS decided_count,
    sum(extract(epoch FROM (a.decided_at - a.requested_at)) / 3600.0)
        FILTER (WHERE a.decided_at IS NOT NULL) AS decision_hours_sum,
    avg(extract(epoch FROM (a.decided_at - a.requested_at)) / 3600.0) AS avg_decision_hours
FROM ticketpilot.approvals AS a
JOIN ticketpilot.ticket_origins AS t
    ON t.tenant_id = a.tenant_id AND t.ticket_id = a.ticket_id
GROUP BY a.tenant_id, t.data_origin, a.status;

CREATE VIEW ticketpilot.daily_ticket_volume AS
SELECT
    tenant_id,
    data_origin,
    (created_at AT TIME ZONE 'UTC')::date AS day,
    count(*) AS created_count,
    count(*) FILTER (WHERE category = 'REFUND') AS refund_count,
    count(*) FILTER (WHERE status = 'RESOLVED') AS resolved_count,
    count(*) FILTER (WHERE status = 'FAILED') AS failed_count,
    avg(extract(epoch FROM (resolved_at - created_at)) / 60.0)
        FILTER (WHERE resolved_at IS NOT NULL) AS avg_resolution_minutes
FROM ticketpilot.ticket_origins
GROUP BY tenant_id, data_origin, day;

CREATE VIEW ticketpilot.tenant_overview AS
WITH order_stats AS (
    SELECT
        tenant_id,
        count(*) AS order_count,
        count(DISTINCT customer_id) AS customer_count,
        sum(paid_amount) AS paid_total,
        count(*) FILTER (WHERE payment_status IN ('PARTIALLY_REFUNDED', 'REFUNDED'))
            AS refunded_order_count
    FROM ticketpilot.orders
    GROUP BY tenant_id
),
ticket_stats AS (
    SELECT
        tenant_id,
        count(*) AS ticket_count,
        count(*) FILTER (WHERE category = 'REFUND') AS refund_ticket_count,
        count(*) FILTER (WHERE status IN ('WAITING_APPROVAL', 'WAITING_INFORMATION'))
            AS open_ticket_count,
        avg(extract(epoch FROM (resolved_at - created_at)) / 60.0)
            FILTER (WHERE resolved_at IS NOT NULL) AS avg_resolution_minutes
    FROM ticketpilot.tickets
    GROUP BY tenant_id
)
SELECT
    o.tenant_id,
    o.order_count,
    o.customer_count,
    o.paid_total,
    o.refunded_order_count,
    coalesce(t.ticket_count, 0) AS ticket_count,
    round(coalesce(t.ticket_count, 0)::numeric / o.order_count, 4) AS contact_rate,
    coalesce(t.refund_ticket_count, 0) AS refund_ticket_count,
    coalesce(t.open_ticket_count, 0) AS open_ticket_count,
    t.avg_resolution_minutes
FROM order_stats AS o
LEFT JOIN ticket_stats AS t ON t.tenant_id = o.tenant_id;

CREATE VIEW ticketpilot.open_backlog_aging AS
SELECT
    tenant_id,
    data_origin,
    status,
    CASE
        WHEN now() - updated_at < interval '1 day' THEN '0-1d'
        WHEN now() - updated_at < interval '3 days' THEN '1-3d'
        WHEN now() - updated_at < interval '7 days' THEN '3-7d'
        ELSE '7d+'
    END AS age_bucket,
    count(*) AS ticket_count
FROM ticketpilot.ticket_origins
WHERE status IN ('WAITING_APPROVAL', 'WAITING_INFORMATION')
GROUP BY tenant_id, data_origin, status, age_bucket;

CREATE VIEW ticketpilot.data_origin_summary AS
SELECT
    d.data_origin,
    (SELECT count(*) FROM ticketpilot.ticket_origins AS t WHERE t.data_origin = d.data_origin)
        AS ticket_count,
    (
        SELECT count(*)
        FROM ticketpilot.ticket_messages AS m
        JOIN ticketpilot.ticket_origins AS t
            ON t.tenant_id = m.tenant_id AND t.ticket_id = m.ticket_id
        WHERE t.data_origin = d.data_origin
    ) AS message_count,
    (
        SELECT count(*)
        FROM ticketpilot.approvals AS a
        JOIN ticketpilot.ticket_origins AS t
            ON t.tenant_id = a.tenant_id AND t.ticket_id = a.ticket_id
        WHERE t.data_origin = d.data_origin
    ) AS approval_count,
    (
        SELECT count(*)
        FROM ticketpilot.audit_events AS e
        JOIN ticketpilot.ticket_origins AS t
            ON t.tenant_id = e.tenant_id AND t.ticket_id = e.ticket_id
        WHERE t.data_origin = d.data_origin
    ) AS audit_event_count
FROM (VALUES ('SYNTHETIC_HISTORY'), ('LIVE_RUN')) AS d (data_origin);
