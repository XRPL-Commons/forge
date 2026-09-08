"""Bounded authoritative closure reads when work leaves the open snapshot.

A missing item is only a refresh trigger. This module never infers completion,
advances collection coverage, writes GitHub, or runs from the dashboard.
"""
import fcntl
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import time

from .control import atomic_json
from .observability import connect, normalized, now as stamp, save_item

MAX_REQUESTS = 4
MAX_SECONDS = 4.0
WINDOW_SECONDS = 60
MAX_PENDING = 100


def _date(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def _pending(value):
    return {str(row['number']): {'number': row['number'], 'kind': row['kind']}
            for row in value if isinstance(row, dict) and type(row.get('number')) is int
            and row['number'] > 0 and row.get('kind') in ('issue', 'pr')}


def refresh(state, repo, open_items, previous=None, *, run=subprocess.run, now=None):
    """Best effort enrichment; callers retain successful admission snapshots."""
    state = Path(state)
    moment = time.time() if now is None else now
    present = {item['number'] for item in open_items}
    previous = previous or {}
    pending = _pending(previous.get('completion_refresh', {}).get('pending', []))
    details = previous.get('item_details', {})
    pending.update(_pending(list(details.values()) if isinstance(details, dict) else []))
    pending = {key: row for key, row in pending.items() if row['number'] not in present}
    result = {'status': 'deferred', 'updated': [], 'errors': [], 'pending': list(pending.values())[:MAX_PENDING]}
    queue_path = state / 'completion-refresh' / (hashlib.sha256(repo.lower().encode()).hexdigest() + '.json')
    db = None
    try:
        state.mkdir(parents=True, exist_ok=True)
        with (state / 'completion-refresh.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return result
            if queue_path.exists():
                saved = _pending(json.loads(queue_path.read_text()).get('pending', []))
                saved.update(pending)
                pending = saved
            # Also catch work that vanished before this feature was installed.
            existing = connect(state.absolute())
            if existing:
                try:
                    for row in existing.execute("SELECT number,kind FROM items WHERE repo=? AND stage IN ('build','review','audit') AND json_extract(data,'$.state')='open' ORDER BY observed_at DESC LIMIT 100", (repo,)):
                        pending.setdefault(str(row['number']), {'number': row['number'], 'kind': row['kind']})
                finally:
                    existing.close()
            pending = {key: row for key, row in pending.items() if row['number'] not in present}
            pending = dict(list(pending.items())[:MAX_PENDING])
            result['pending'] = list(pending.values())
            atomic_json(queue_path, {'repo': repo, 'pending': result['pending']})
            if not pending:
                result['status'] = 'idle'
                return result
            with (state / 'observability.lock').open('a') as observer_lock:
                try:
                    fcntl.flock(observer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    result['status'] = 'collector_busy'
                    return result
                budget_path = state / 'completion-budget.json'
                budget = json.loads(budget_path.read_text()) if budget_path.exists() else {}
                if 'window' not in budget or not 0 <= moment - budget.get('window', 0) < WINDOW_SECONDS:
                    budget = {'window': moment, 'requests': 0, 'seconds': 0.0}
                db = connect(state, write=True)
                for key, item in list(pending.items())[:2]:
                    remaining = MAX_SECONDS - budget.get('seconds', 0)
                    if budget.get('requests', 0) >= MAX_REQUESTS or remaining < 0.1:
                        result['status'] = 'budget_deferred'
                        break
                    number, kind = item['number'], item['kind']
                    endpoint = f'repos/{repo}/' + ('pulls' if kind == 'pr' else 'issues') + f'/{number}'
                    started = time.monotonic()
                    budget['requests'] = budget.get('requests', 0) + 1
                    try:
                        response = run(['gh', 'api', endpoint], capture_output=True, text=True,
                                       timeout=min(2.0, remaining))
                        if response.returncode or len(response.stdout) > 1024 * 1024:
                            raise ValueError('closure_read_failed')
                        raw = json.loads(response.stdout)
                        expected_url = f'https://github.com/{repo}/' + ('pull' if kind == 'pr' else 'issues') + f'/{number}'
                        if (type(raw.get('number')) is not int or raw['number'] != number
                                or str(raw.get('html_url', '')).lower() != expected_url.lower()
                                or (kind == 'issue' and 'pull_request' in raw)
                                or (kind == 'pr' and ('merged_at' not in raw or not isinstance(raw.get('head'), dict)))):
                            raise ValueError('closure_identity_invalid')
                        if raw.get('state') == 'open':
                            pending.pop(key)
                            continue
                        if raw.get('state') != 'closed' or not _date(raw.get('closed_at')):
                            raise ValueError('closure_unconfirmed')
                        if kind == 'pr':
                            if (type(raw.get('merged')) is not bool
                                    or (raw['merged'] and not _date(raw.get('merged_at')))
                                    or (not raw['merged'] and raw.get('merged_at') is not None)):
                                raise ValueError('merge_state_unconfirmed')
                            raw['pull_request'] = {'url': raw.get('url'), 'html_url': raw['html_url']}
                        prior = db.execute('SELECT data,evidence_updated_at FROM items WHERE repo=? AND number=?', (repo, number)).fetchone()
                        old = json.loads(prior['data']) if prior else {}
                        data = {**old, **normalized(repo, raw, old)}
                        # A targeted lifecycle read does not replace full review,
                        # audit, summary, or check evidence collected separately.
                        for field in ('evidence', 'evidence_status', 'review_decision', 'checks', 'linked_issues'):
                            if field in old:
                                data[field] = old[field]
                        if kind == 'pr':
                            data['raw_pr'] = raw
                            data['head_sha'] = raw['head'].get('sha')
                            data['base_sha'] = (raw.get('base') or {}).get('sha')
                        data['completion_source'] = 'targeted_github_lifecycle'
                        data['completion_observed_at'] = stamp()
                        with db:
                            save_item(db, repo, data, data['completion_observed_at'],
                                      prior['evidence_updated_at'] if prior else None)
                        result['updated'].append(number)
                        pending.pop(key)
                        result['status'] = 'updated'
                    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                        result['errors'].append({'number': number, 'reason': type(exc).__name__})
                    finally:
                        budget['seconds'] = budget.get('seconds', 0) + time.monotonic() - started
                        atomic_json(budget_path, budget)
                result['pending'] = list(pending.values())
                atomic_json(queue_path, {'repo': repo, 'pending': result['pending']})
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        result['status'] = 'unavailable'
    finally:
        if db:
            db.close()
    return result
