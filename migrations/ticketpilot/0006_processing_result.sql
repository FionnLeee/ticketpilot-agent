ALTER TABLE ticketpilot.tickets
    ADD COLUMN processing_result text;

UPDATE ticketpilot.tickets
SET processing_result = CASE status
    WHEN 'WAITING_APPROVAL' THEN 'WAITING_APPROVAL'
    WHEN 'WAITING_INFORMATION' THEN 'NEEDS_INPUT'
    WHEN 'RESOLVED' THEN 'ANSWERED'
    WHEN 'FAILED' THEN 'PROCESSING_FAILED'
    ELSE NULL
END;

ALTER TABLE ticketpilot.tickets
    ADD CONSTRAINT tickets_processing_result_check CHECK (
        (status IN ('NEW', 'PROCESSING') AND processing_result IS NULL)
        OR (status = 'WAITING_APPROVAL' AND processing_result = 'WAITING_APPROVAL')
        OR (status = 'WAITING_INFORMATION' AND processing_result = 'NEEDS_INPUT')
        OR (status = 'RESOLVED' AND processing_result = 'ANSWERED')
        OR (
            status = 'FAILED'
            AND processing_result IN (
                'DEPENDENCY_FAILED',
                'INSUFFICIENT_EVIDENCE',
                'PROCESSING_FAILED'
            )
        )
    );
