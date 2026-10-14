-- Successful physical reservations are the durable allocation order. Filter out
-- observation and result events before looking up the exact reserved job role.
CREATE INDEX unit_reservation_events ON events(unit_id,sequence DESC,job_id)
WHERE kind='attempt_reserved';
