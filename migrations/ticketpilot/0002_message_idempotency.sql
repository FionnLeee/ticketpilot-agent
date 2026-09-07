ALTER TABLE ticketpilot.ticket_messages
    ADD COLUMN idempotency_key text;

ALTER TABLE ticketpilot.ticket_messages
    ADD CONSTRAINT ticket_messages_idempotency_key_length CHECK (
        idempotency_key IS NULL OR char_length(idempotency_key) BETWEEN 1 AND 128
    );

CREATE UNIQUE INDEX ticket_messages_tenant_ticket_idempotency_idx
    ON ticketpilot.ticket_messages (tenant_id, ticket_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
