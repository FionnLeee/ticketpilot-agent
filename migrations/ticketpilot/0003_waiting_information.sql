ALTER TABLE ticketpilot.tickets DROP CONSTRAINT tickets_status_check;
ALTER TABLE ticketpilot.tickets ADD CONSTRAINT tickets_status_check CHECK (
    status IN ('NEW', 'PROCESSING', 'WAITING_APPROVAL', 'WAITING_INFORMATION', 'RESOLVED', 'FAILED')
);
ALTER TABLE ticketpilot.tickets ADD COLUMN pending_request jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(pending_request) = 'object');
