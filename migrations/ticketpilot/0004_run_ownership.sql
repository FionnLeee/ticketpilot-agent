ALTER TABLE ticketpilot.tickets
    ADD COLUMN active_run_id uuid,
    ADD COLUMN trigger_message_id uuid,
    ADD COLUMN run_started boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT tickets_run_ownership_check CHECK (
        active_run_id IS NOT NULL OR (trigger_message_id IS NULL AND NOT run_started)
    );

CREATE UNIQUE INDEX ticket_customer_message_run_idx
    ON ticketpilot.ticket_messages (tenant_id, ticket_id, run_id)
    WHERE role IN ('CUSTOMER', 'STAFF') AND run_id IS NOT NULL;
