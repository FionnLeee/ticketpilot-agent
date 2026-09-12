ALTER TABLE ticketpilot.tickets
    ADD COLUMN create_request_actor_id text,
    ADD COLUMN create_idempotency_key text,
    ADD COLUMN create_request_hash char(64),
    ADD CONSTRAINT tickets_create_idempotency_fields_check CHECK (
        (create_request_actor_id IS NULL
         AND create_idempotency_key IS NULL
         AND create_request_hash IS NULL)
        OR
        (create_request_actor_id IS NOT NULL
         AND create_idempotency_key IS NOT NULL
         AND char_length(create_idempotency_key) BETWEEN 1 AND 128
         AND create_request_hash ~ '^[0-9a-f]{64}$')
    );

CREATE UNIQUE INDEX tickets_create_request_idempotency_idx
    ON ticketpilot.tickets (tenant_id, create_request_actor_id, create_idempotency_key)
    WHERE create_idempotency_key IS NOT NULL;

ALTER TABLE ticketpilot.approvals
    ADD COLUMN action_id uuid;

UPDATE ticketpilot.approvals
SET action_id = run_id
WHERE action_id IS NULL;

ALTER TABLE ticketpilot.approvals
    ALTER COLUMN action_id SET NOT NULL,
    ADD CONSTRAINT approvals_tenant_action_id_key UNIQUE (tenant_id, action_id);
