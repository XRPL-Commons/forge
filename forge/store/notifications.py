"""Per-channel outbox writes and durable attempts; delivery remains at least once."""
import hashlib
import json
import uuid

from .runtime import writable
from .supporting_queries import SupportingQueries


def enqueue(store, message, *, now, event_key=None):
    if not isinstance(message, str) or not message.strip() or len(message.encode()) > 60000:
        raise ValueError('notification must contain a bounded message')
    event_id = hashlib.sha256(event_key.encode()).hexdigest() if event_key else uuid.uuid4().hex
    with store.write() as db:
        writable(db)
        for channel in ('ntfy', 'gchat', 'relay'):
            identifier = event_id + ':' + channel
            db.execute('INSERT INTO notification_outbox(id,enqueue_key,channel,recipient_ref,payload_json,status,retry_at,created_at,'
                       'source_event_id,delivery_state,attempt_count) '
                       "VALUES(?,?,?,?,?,'pending',0,?,?,'pending',0) ON CONFLICT(enqueue_key) DO NOTHING",
                       (identifier, identifier, channel, channel, json.dumps({'message': message}, allow_nan=False), now, event_id))
    return event_id


def drain(store, sender, *, now, limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('notification batch must contain one to one hundred events')
    with store.read_snapshot() as db:
        writable(db)
        identifiers = [r[0] for r in db.execute("SELECT source_event_id FROM notification_outbox WHERE status IN ('pending','sending') "
            "AND delivery_state IN ('pending','retry') AND retry_at<=? GROUP BY source_event_id ORDER BY MIN(created_at),source_event_id LIMIT ?", (now, limit))]
    for event_id in identifiers:
        event = SupportingQueries(store).notification(event_id)
        for channel, receipt in event['channels'].items():
            if receipt['status'] not in ('pending', 'retry') or receipt['next_at'] > now:
                continue
            with store.write() as db:
                writable(db)
                row = db.execute('SELECT * FROM notification_outbox WHERE source_event_id=? AND channel=?', (event_id, channel)).fetchone()
                attempt = row['attempt_count'] + 1
                delivery_id = uuid.uuid4().hex
                db.execute("UPDATE notification_outbox SET status='sending',attempt_count=?,last_attempt_at=? WHERE id=?", (attempt, now, row['id']))
                db.execute("INSERT INTO notification_deliveries(id,notification_id,attempt_number,status,attempted_at) VALUES(?,?,?,'uncertain',?)",
                           (delivery_id, row['id'], attempt, now))
            try:
                status = sender(channel, event)
            except Exception:
                status = 'failed'
            done = status in ('accepted_by_relay', 'disabled')
            state = status if done else 'failed' if attempt >= 8 else 'retry'
            with store.write() as db:
                writable(db)
                db.execute('UPDATE notification_outbox SET status=?,delivery_state=?,retry_at=? WHERE id=?',
                           ('delivered' if done else 'failed' if state == 'failed' else 'pending', state,
                            now + min(3600, 30 * 2 ** min(attempt - 1, 7)), row['id']))
                db.execute('UPDATE notification_deliveries SET status=?,receipt_ref=?,error_code=? WHERE id=?',
                           ('confirmed' if done else 'failed', status if done else None, None if done else 'delivery_failed', delivery_id))
    with store.write() as db:
        writable(db)
        db.execute("INSERT INTO runtime_controls(name,heartbeat_at,updated_at,details_json) VALUES('notification_delivery',?,?,?) "
                   'ON CONFLICT(name) DO UPDATE SET heartbeat_at=excluded.heartbeat_at,updated_at=excluded.updated_at,details_json=excluded.details_json',
                   (now, now, json.dumps({'last_run': now, 'processed': len(identifiers), 'quarantined': []})))
    return len(identifiers)
