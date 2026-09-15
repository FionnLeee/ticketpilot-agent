CREATE INDEX tickets_tenant_customer_updated_idx
    ON ticketpilot.tickets (tenant_id, customer_id, updated_at DESC);
