"""Durable, at-least-once notification outbox. Enqueue never accesses the network.

Run `python -m forge.launch forge.notifications drain --state DIR` from an independent timer.
Credentials arrive in that service's environment, never in event files. A successful
relay request means accepted_by_relay, not confirmed phone delivery.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import uuid

CHANNELS = ('ntfy', 'gchat', 'relay')
MAX_ATTEMPTS = 8


def runtime_store(state):
    from .store.runtime import selected
    state = Path(state).absolute()
    return selected(state.parent.parent) if state.name == 'notifications' and state.parent.name == 'state' else None


def atomic_json(path, value):
    from .legacy_writers import mutation
    with mutation(path):
        _atomic_json(path, value)


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.event-')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def enqueue(state, message, now=None, event_key=None):
    if runtime_store(state) is not None:
        return _enqueue(state, message, now=now, event_key=event_key)
    from .legacy_writers import mutation
    with mutation(Path(state) / "outbox/event.json"):
        return _enqueue(state, message, now=now, event_key=event_key)


def _enqueue(state, message, now=None, event_key=None):
    store = runtime_store(state)
    if store is not None:
        from .store.notifications import enqueue as sql_enqueue
        return sql_enqueue(store, message, now=time.time() if now is None else now, event_key=event_key)
    if not message.strip():
        raise ValueError('message cannot be empty')
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    # Serialize concurrent check-and-create calls. Re-enqueueing the same
    # incident must never replace a record that already has delivery receipts.
    with (state / 'enqueue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        event_id = hashlib.sha256(event_key.encode()).hexdigest() if event_key else uuid.uuid4().hex
        existing = state / 'outbox' / (event_id + '.json')
        if existing.exists():
            return event_id
        event = {'id': event_id, 'created_at': time.time() if now is None else now,
                 'message': message,
                 'channels': {c: {'status': 'pending', 'attempts': 0, 'next_at': 0} for c in CHANNELS}}
        atomic_json(existing, event)
        return event_id


def deliver(channel, event, env=None, runner=subprocess.run):
    env = os.environ if env is None else env
    message = event['message']
    if channel == 'ntfy':
        if not env.get('NTFY_TOPIC'):
            return 'disabled'
        cmd = ['curl', '--fail', '--silent', '--show-error', '--connect-timeout', '5',
               '--max-time', '15', '-H', 'Title: forge', '--data-binary', '@-',
               'https://ntfy.sh/' + env['NTFY_TOPIC']]
        payload = message
    elif channel == 'gchat':
        if not env.get('GCHAT_WEBHOOK'):
            return 'disabled'
        cmd = ['curl', '--fail', '--silent', '--show-error', '--connect-timeout', '5',
               '--max-time', '15', '-H', 'Content-Type: application/json',
               '--data-binary', '@-', env['GCHAT_WEBHOOK']]
        payload = json.dumps({'text': message})
    elif channel == 'relay':
        names = ('RELAY_HOST', 'RELAY_WORKER_CONTAINER', 'NOTIFY_JID', 'NOTIFY_CHANNEL',
                 'NOTIFY_QUEUE', 'RELAY_REDIS_HOST', 'RELAY_REDIS_PORT')
        if not all(env.get(key) for key in names):
            return 'disabled'
        # Values are shell-quoted because SSH passes its remote command through a shell.
        code = '(async()=>{const {Queue}=await import("bullmq");const e=JSON.parse(Buffer.from(process.env.FORGE_EVENT,"base64").toString());const q=new Queue(e.queue,{connection:{host:e.host,port:Number(e.port)}});await q.add("notify",{jid:e.jid,text:e.text,channel:e.channel},{jobId:e.id});await q.close()})().catch(()=>process.exit(1))'
        data = {'queue': env['NOTIFY_QUEUE'], 'host': env['RELAY_REDIS_HOST'],
                'port': env['RELAY_REDIS_PORT'], 'jid': env['NOTIFY_JID'],
                'channel': env['NOTIFY_CHANNEL'], 'text': message, 'id': event['id']}
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        remote = shlex.join(['docker', 'exec', '-e', 'FORGE_EVENT=' + encoded,
                             env['RELAY_WORKER_CONTAINER'], 'node', '-e', code])
        cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
               '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
               '--', env['RELAY_HOST'], remote]
        payload = ''
    else:
        raise ValueError('unknown channel')
    try:
        result = runner(cmd, input=payload, text=True, capture_output=True, timeout=20, check=False)
        return 'accepted_by_relay' if result.returncode == 0 else 'failed'
    except (subprocess.TimeoutExpired, OSError):
        return 'failed'


def drain(state, sender=deliver, now=None, limit=2):
    if runtime_store(state) is not None:
        return _drain(state, sender=sender, now=now, limit=limit)
    from .legacy_writers import mutation
    with mutation(Path(state) / "outbox/event.json"):
        return _drain(state, sender=sender, now=now, limit=limit)


def _drain(state, sender=deliver, now=None, limit=2):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    with (state / 'delivery.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        store = runtime_store(state)
        if store is not None:
            from .store.notifications import drain as sql_drain
            return sql_drain(store, sender, now=now, limit=limit)
        processed = 0
        quarantined = []
        for path in sorted((state / 'outbox').glob('*.json')):
            if processed >= limit:
                break
            try:
                event = json.loads(path.read_text())
                if (not isinstance(event.get('message'), str)
                        or not isinstance(event.get('id'), str)
                        or set(event.get('channels', {})) != set(CHANNELS)):
                    raise ValueError('invalid event envelope')
                for record in event['channels'].values():
                    if (record['status'] not in ('pending', 'retry', 'failed', 'accepted_by_relay', 'disabled')
                            or not isinstance(record['attempts'], int)
                            or not isinstance(record['next_at'], (int, float))):
                        raise ValueError('invalid channel state')
            except (ValueError, KeyError, TypeError, AttributeError):
                quarantine = state / 'quarantine'
                quarantine.mkdir(exist_ok=True)
                os.replace(path, quarantine / path.name)
                quarantined.append(path.name)
                continue
            due = [c for c, record in event['channels'].items()
                   if record['status'] in ('pending', 'retry') and record['next_at'] <= now]
            if not due:
                continue
            # One slow delivery cannot delay other channels; the controller never waits here.
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {c: pool.submit(sender, c, event) for c in due}
                for channel, future in futures.items():
                    try:
                        status = future.result()
                    except Exception:
                        status = 'failed'
                    record = event['channels'][channel]
                    record['attempts'] += 1
                    if status in ('accepted_by_relay', 'disabled'):
                        record['status'] = status
                    else:
                        record['status'] = 'failed' if record['attempts'] >= MAX_ATTEMPTS else 'retry'
                    record['next_at'] = now + min(3600, 30 * 2 ** (record['attempts'] - 1))
                    record['last_attempt_at'] = now
                    atomic_json(path, event)
            processed += 1
        atomic_json(state / "delivery-status.json", {"last_run": now, "processed": processed, "quarantined": quarantined})
        return processed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['enqueue', 'drain'])
    parser.add_argument('--state', default=os.environ.get('FORGE_NOTIFY_STATE', 'state/notifications'))
    parser.add_argument('--event-key')
    parser.add_argument('message', nargs='*')
    args = parser.parse_intermixed_args(argv)
    if args.action == 'enqueue':
        print(enqueue(args.state, ' '.join(args.message), event_key=args.event_key))
    else:
        drain(args.state)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
