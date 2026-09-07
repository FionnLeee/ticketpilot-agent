CREATE TABLE ticketpilot.orders (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    order_reference text NOT NULL,
    customer_id text NOT NULL,
    payment_status text NOT NULL CHECK (
        payment_status IN ('PENDING', 'PAID', 'PARTIALLY_REFUNDED', 'REFUNDED', 'CANCELLED')
    ),
    fulfillment_status text NOT NULL CHECK (
        fulfillment_status IN ('PENDING', 'PROCESSING', 'SHIPPED', 'DELIVERED', 'CANCELLED')
    ),
    paid_amount numeric(12, 2) NOT NULL CHECK (paid_amount >= 0),
    currency char(3) NOT NULL DEFAULT 'CNY' CHECK (currency ~ '^[A-Z]{3}$'),
    refundable_amount numeric(12, 2) NOT NULL CHECK (
        refundable_amount >= 0 AND refundable_amount <= paid_amount
    ),
    carrier text,
    tracking_number text,
    estimated_delivery_at timestamptz,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, order_reference)
);

CREATE TABLE ticketpilot.tickets (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    customer_id text NOT NULL,
    order_pk uuid,
    thread_id text NOT NULL UNIQUE,
    status text NOT NULL CHECK (
        status IN ('NEW', 'PROCESSING', 'WAITING_APPROVAL', 'RESOLVED', 'FAILED')
    ),
    category text CHECK (category IN ('ORDER_STATUS', 'REFUND', 'POLICY', 'OTHER')),
    priority text CHECK (priority IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')),
    risk_level text CHECK (
        risk_level IN ('READ_ONLY', 'LOW_RISK_WRITE', 'HIGH_RISK_WRITE')
    ),
    subject text NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 200),
    resolution_summary text,
    triaged_at timestamptz,
    resolved_at timestamptz,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, order_pk) REFERENCES ticketpilot.orders (tenant_id, id)
);

CREATE TABLE ticketpilot.ticket_messages (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    ticket_id uuid NOT NULL,
    run_id uuid,
    role text NOT NULL CHECK (role IN ('CUSTOMER', 'AGENT', 'STAFF')),
    content text NOT NULL CHECK (char_length(content) BETWEEN 1 AND 8000),
    citations jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(citations) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES ticketpilot.tickets (tenant_id, id)
);

CREATE TABLE ticketpilot.approvals (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    ticket_id uuid NOT NULL,
    run_id uuid NOT NULL,
    action_type text NOT NULL CHECK (action_type IN ('REFUND')),
    action_payload jsonb NOT NULL CHECK (jsonb_typeof(action_payload) = 'object'),
    status text NOT NULL CHECK (
        status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXECUTED', 'CANCELLED')
    ),
    requested_by text NOT NULL,
    decided_by text,
    decision_reason text,
    idempotency_key text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    executed_at timestamptz,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES ticketpilot.tickets (tenant_id, id),
    CHECK (
        (status = 'PENDING' AND decided_by IS NULL AND decided_at IS NULL)
        OR (status <> 'PENDING')
    ),
    CHECK (
        status NOT IN ('APPROVED', 'REJECTED', 'EXECUTED')
        OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)
    ),
    CHECK (status <> 'EXECUTED' OR executed_at IS NOT NULL)
);

CREATE TABLE ticketpilot.audit_events (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    ticket_id uuid,
    run_id uuid,
    approval_id uuid,
    actor_type text NOT NULL CHECK (actor_type IN ('CUSTOMER', 'AGENT', 'STAFF', 'SYSTEM')),
    actor_id text,
    event_type text NOT NULL,
    node_name text,
    tool_name text,
    outcome text NOT NULL CHECK (outcome IN ('STARTED', 'SUCCEEDED', 'FAILED', 'BLOCKED')),
    details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES ticketpilot.tickets (tenant_id, id),
    FOREIGN KEY (tenant_id, approval_id) REFERENCES ticketpilot.approvals (tenant_id, id)
);

CREATE INDEX tickets_tenant_status_updated_idx
    ON ticketpilot.tickets (tenant_id, status, updated_at DESC);
CREATE INDEX ticket_messages_ticket_created_idx
    ON ticketpilot.ticket_messages (tenant_id, ticket_id, created_at);
CREATE INDEX approvals_tenant_status_requested_idx
    ON ticketpilot.approvals (tenant_id, status, requested_at DESC);
CREATE INDEX audit_events_ticket_occurred_idx
    ON ticketpilot.audit_events (tenant_id, ticket_id, occurred_at);
CREATE INDEX audit_events_run_occurred_idx
    ON ticketpilot.audit_events (tenant_id, run_id, occurred_at);
