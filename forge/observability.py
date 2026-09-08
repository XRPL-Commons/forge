"""Read-only GitHub collection and deterministic local pipeline history.

Lifecycle dates are GitHub facts; events are observations, never inferred history.
"""
import argparse
import fcntl
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from urllib.parse import urlencode

STAGES = ('pending', 'build', 'review', 'audit', 'held', 'completed', 'closed', 'unknown')

EVIDENCE_DUE = "(evidence_updated_at IS NULL OR (kind='pr' AND json_extract(data, '$.state')='open' AND evidence_updated_at<=?))"


def evidence_cutoff(observed=None):
    timestamp = datetime.fromisoformat((observed or now()).replace('Z', '+00:00'))
    return (timestamp - timedelta(minutes=10)).isoformat(timespec='seconds').replace('+00:00', 'Z')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def cutoff(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds').replace('+00:00', 'Z')


def excerpt(body, limit=600):
    return ' '.join(str(body or '').split())[:limit]


def classify(item):
    labels = {x['name'] if isinstance(x, dict) else x for x in item.get('labels', [])}
    if item.get('state') == 'closed':
        if 'pull_request' in item and 'merged_at' not in item:
            return 'unknown'
        return 'completed' if (item.get('merged_at') if 'pull_request' in item else item.get('state_reason') == 'completed') else 'closed'
    if 'hold' in labels:
        return 'held'
    stages = labels & {'ready-for-review', 'ready-for-audit', 'changes-requested'}
    if len(stages) > 1:
        return 'unknown'
    if 'ready-for-audit' in labels:
        return 'audit'
    if 'ready-for-review' in labels:
        return 'review'
    if 'wip' in labels or 'changes-requested' in labels:
        return 'build'
    return 'unknown' if 'pull_request' in item else 'pending'


class GitHub:
    def get(self, endpoint, pages=False):
        command = ['gh', 'api'] + (['--paginate', '--slurp'] if pages else []) + [endpoint]
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError(f'GitHub read failed (exit {result.returncode})')
        value = json.loads(result.stdout)
        return [item for page in value for item in page] if pages else value


    def pr_summary(self, repo, number):
        result = subprocess.run(['gh', 'pr', 'view', str(number), '--repo', repo,
            '--json', 'reviewDecision,statusCheckRollup,mergeStateStatus,headRefOid,closingIssuesReferences'],
            capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise RuntimeError(f'GitHub PR summary unavailable (exit {result.returncode})')
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError('Expected PR summary object')
        return value


def summarize_pr(client, repo, data):
    """GitHub aggregate policy facts are not proof of Forge human authorization."""
    data.update(review_decision='UNKNOWN', merge_state='UNKNOWN', checks=None,
                summary_status='unavailable', summary_error=None)
    try:
        if not hasattr(client, 'pr_summary'):
            raise RuntimeError('PR summary reader unavailable')
        raw = client.pr_summary(repo, data['number'])
        data['raw_pr_summary'] = raw
        data['summary_head_sha'] = raw.get('headRefOid')
        data['summary_observed_at'] = now()
        if not raw.get('headRefOid') or raw['headRefOid'] != data.get('head_sha'):
            data['summary_error'] = 'PR head changed or unavailable during collection'
            return
        data['review_decision'] = raw.get('reviewDecision') or 'UNKNOWN'
        data['merge_state'] = raw.get('mergeStateStatus') or 'UNKNOWN'
        rollup = raw.get('statusCheckRollup')
        if isinstance(rollup, list):
            counts = dict(total=len(rollup), failed=0, pending=0, passed=0, skipped=0, unknown=0)
            for check in rollup:
                status = check.get('status')
                outcome = check.get('conclusion') if status == 'COMPLETED' else check.get('state')
                if status and status != 'COMPLETED':
                    category = 'pending' if status in ('QUEUED', 'IN_PROGRESS', 'WAITING', 'REQUESTED', 'PENDING') else 'unknown'
                elif outcome in ('FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'STALE'):
                    category = 'failed'
                elif outcome == 'SUCCESS':
                    category = 'passed'
                elif outcome in ('NEUTRAL', 'SKIPPED'):
                    category = 'skipped'
                elif outcome in ('PENDING', 'EXPECTED'):
                    category = 'pending'
                else:
                    category = 'unknown'
                counts[category] += 1
            data['checks'] = counts
        data['summary_status'] = 'ok'
    except (RuntimeError, ValueError, KeyError, AttributeError, OSError, subprocess.TimeoutExpired) as exc:
        data.update(review_decision='UNKNOWN', merge_state='UNKNOWN', checks=None,
                    summary_error=type(exc).__name__)


def connect(state, write=False):
    path = Path(state) / 'observability.sqlite3'
    if not write and not path.exists():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
    db = sqlite3.connect(str(path) if write else path.as_uri() + '?mode=ro', uri=not write, timeout=5)
    db.row_factory = sqlite3.Row
    if write:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS repositories(repo TEXT PRIMARY KEY, observed_at TEXT,
            coverage_since TEXT, error TEXT, status TEXT, cursor TEXT);
        CREATE TABLE IF NOT EXISTS items(repo TEXT, number INTEGER, kind TEXT, stage TEXT,
            updated_at TEXT, observed_at TEXT, evidence_updated_at TEXT, data TEXT,
            PRIMARY KEY(repo,number));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, repo TEXT, number INTEGER,
            kind TEXT, stage TEXT, previous_stage TEXT, observed_at TEXT, summary TEXT, url TEXT);
        CREATE INDEX IF NOT EXISTS event_time ON events(observed_at);
        ''')
    return db


def normalized(repo, raw, previous=None):
    kind = 'pr' if 'pull_request' in raw else 'issue'
    data = {k: raw.get(k) for k in ('number', 'title', 'created_at', 'updated_at', 'closed_at', 'merged_at', 'state', 'state_reason', 'body')}
    data.update(repo=repo, kind=kind, stage=classify(raw), url=raw.get('html_url'),
                labels=[x['name'] if isinstance(x, dict) else x for x in raw.get('labels', [])],
                summary=excerpt(raw.get('body')), raw=raw,
                evidence=(previous or {}).get('evidence', []), evidence_status='pending',
                review_decision=None, checks=None, linked_issues=[])
    if previous:
        for key in ('raw_pr', 'head_sha', 'base_sha', 'linked_issues'):
            if key in previous:
                data[key] = previous[key]
        if previous.get('merged_at') and raw.get('state') == 'closed':
            data['merged_at'] = previous['merged_at']
            data['stage'] = 'completed'
    return data



def classify_evidence(item):
    # Audit headings and explicit criticality lines identify audit-related content,
    # including Markdown used by the live auditor. They never certify a pass.
    marker = r'(?im)^\s*(?:#{1,6}\s*)?(?:\*{1,2})?(?:Criticality:\s*(?:NORMAL|CRITICAL|UNKNOWN)\b|Security audit\b|Criticality classification\b)'
    if item.get('kind') != 'audit' and re.search(marker, item.get('body', '')):
        item['source_kind'] = item['kind']
        item['kind'] = 'audit'
    elif item.get('kind') == 'comment' and re.search(
            r'(?im)^\s*(?:#{1,6}\s*)?(?:\*{1,2})?Review(?:er)?\s*[:—-]\s*(?:PASS|FAIL|changes requested)\b', item.get('body', '')):
        item['source_kind'] = 'comment'
        item['kind'] = 'review'
    return item

def enrich(client, repo, data):
    number = data['number']
    evidence = []
    if data['kind'] == 'pr':
        detail = client.get(f'repos/{repo}/pulls/{number}')
        data['raw_pr'] = detail
        data['merged_at'] = detail.get('merged_at')
        if data.get('state') == 'closed':
            data['stage'] = 'completed' if data['merged_at'] else 'closed'
        data['head_sha'] = detail.get('head', {}).get('sha')
        data['base_sha'] = detail.get('base', {}).get('sha')
        # Closing-keyword references are explicit textual evidence, not verified links.
        body = detail.get('body') or ''
        data['linked_issues'] = [{'repo': repo, 'number': int(n), 'source': 'closing_keyword',
            'url': f'https://github.com/{repo}/issues/{n}'} for n in sorted(set(re.findall(
                r'(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b', body)), key=int)]
        for review in client.get(f'repos/{repo}/pulls/{number}/reviews?per_page=100', pages=True):
            evidence.append({'kind': 'review', 'id': review['id'], 'author': (review.get('user') or {}).get('login'),
                'created_at': review.get('submitted_at'), 'body': review.get('body') or '',
                'summary': excerpt(review.get('body')), 'url': review.get('html_url'),
                'state': review.get('state'), 'commit_id': review.get('commit_id'), 'raw': review})
    comments = client.get(f'repos/{repo}/issues/{number}/comments?per_page=100', pages=True)
    if data['kind'] == 'pr':
        comments += client.get(f'repos/{repo}/pulls/{number}/comments?per_page=100', pages=True)
    for comment in comments:
        evidence.append({'kind': 'review_comment' if 'diff_hunk' in comment else 'comment', 'id': comment['id'], 'author': (comment.get('user') or {}).get('login'),
            'created_at': comment.get('created_at'), 'body': comment.get('body') or '',
            'summary': excerpt(comment.get('body')), 'url': comment.get('html_url'), 'raw': comment})
    for item in evidence:
        classify_evidence(item)
    data['evidence'] = evidence
    data['evidence_status'] = 'ok'
    if data['kind'] == 'pr':
        summarize_pr(client, repo, data)


def save_item(db, repo, data, observed, evidence_updated=None):
    prior = db.execute('SELECT stage FROM items WHERE repo=? AND number=?', (repo, data['number'])).fetchone()
    refined = False
    if evidence_updated:
        initial = db.execute('SELECT id FROM events WHERE repo=? AND number=? AND observed_at=? ORDER BY id DESC LIMIT 1', (repo, data['number'], observed)).fetchone()
        if initial:
            db.execute('UPDATE events SET stage=? WHERE id=?', (data['stage'], initial['id']))
            refined = True
    if not refined and (prior is None or prior['stage'] != data['stage']):
        db.execute('INSERT INTO events(repo,number,kind,stage,previous_stage,observed_at,summary,url) VALUES(?,?,?,?,?,?,?,?)',
            (repo, data['number'], data['kind'], data['stage'], prior['stage'] if prior else None, observed,
             ('First observed: ' if prior is None else 'Stage observed: ') + data['title'], data['url']))
    db.execute('INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?)',
        (repo, data['number'], data['kind'], data['stage'], data['updated_at'], observed,
         evidence_updated, json.dumps(data)))


def collect(state, repos, days=30, evidence_limit=8, client=None):
    Path(state).mkdir(parents=True, exist_ok=True)
    with (Path(state) / 'observability.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Observability collection already running') from exc
        return _collect(state, repos, days, evidence_limit, client)


def _collect(state, repos, days=30, evidence_limit=8, client=None):
    """Snapshot each repo atomically; evidence backlog resumes independently of cursor.

    Evidence limit is per repository per run (a PR needs four paginated reads plus one summary read).
    Failures retain previous evidence and never advance a failed snapshot cursor.
    """
    client = client or GitHub()
    db = connect(state, write=True)
    results = []
    try:
        for repo in repos:
            if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
                raise ValueError('Invalid repository')
            observed = now()
            evidence_before = evidence_cutoff(observed)
            previous = db.execute('SELECT * FROM repositories WHERE repo=?', (repo,)).fetchone()
            since = (datetime.fromisoformat(previous['cursor'].replace('Z', '+00:00')) - timedelta(minutes=5)).isoformat().replace('+00:00', 'Z') if previous and previous['cursor'] else cutoff(days)
            coverage = previous['coverage_since'] if previous else since
            try:
                # Open inventory is always complete, even on first collection.
                recent = client.get(f'repos/{repo}/issues?' + urlencode({'state': 'all', 'since': since, 'per_page': 100}), pages=True)
                opened = client.get(f'repos/{repo}/issues?state=open&per_page=100', pages=True)
                fetched = {item['number']: item for item in recent + opened}
                with db:
                    for number, raw in fetched.items():
                        old = db.execute('SELECT * FROM items WHERE repo=? AND number=?', (repo, number)).fetchone()
                        if old and old['updated_at'] == raw['updated_at']:
                            db.execute('UPDATE items SET observed_at=? WHERE repo=? AND number=?', (observed, repo, number))
                            continue
                        data = normalized(repo, raw, json.loads(old['data']) if old else None)
                        save_item(db, repo, data, observed)
                    db.execute('INSERT OR REPLACE INTO repositories VALUES(?,?,?,?,?,?)', (repo, observed, coverage, None, 'partial', observed))
                errors = []
                pending = db.execute("SELECT * FROM items WHERE repo=? AND " + EVIDENCE_DUE + " ORDER BY evidence_updated_at IS NOT NULL, evidence_updated_at ASC, CASE WHEN kind='pr' AND json_extract(data, '$.state')='open' THEN 0 WHEN kind='pr' THEN 1 ELSE 2 END, updated_at DESC,number LIMIT ?", (repo, evidence_before, max(0, evidence_limit))).fetchall()
                for row in pending:
                    data = json.loads(row['data'])
                    try:
                        enrich(client, repo, data)
                        with db:
                            save_item(db, repo, data, observed, observed)
                    except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
                        errors.append(f'#{row["number"]}: {type(exc).__name__}')
                remaining = db.execute('SELECT count(*) FROM items WHERE repo=? AND ' + EVIDENCE_DUE, (repo, evidence_before)).fetchone()[0]
                status = 'partial' if remaining else 'ok'
                with db:
                    db.execute('UPDATE repositories SET status=?,error=? WHERE repo=?', (status, '; '.join(errors) or None, repo))
                results.append({'repo': repo, 'status': status, 'pending_evidence': remaining})
            except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
                with db:
                    db.execute('INSERT OR REPLACE INTO repositories VALUES(?,?,?,?,?,?)',
                        (repo, previous['observed_at'] if previous else None, coverage, type(exc).__name__, 'error', previous['cursor'] if previous else None))
                results.append({'repo': repo, 'status': 'error', 'error': type(exc).__name__})
    finally:
        db.close()
    return results


def _read(state, repo=None, days=30, repos=None):
    days = max(1, min(365, int(days)))
    empty = {'status': 'unavailable', 'observed_at': None, 'coverage_since': None, 'errors': [], 'repos': [], 'items': [], 'events': [], 'daily': [], 'history_basis': 'GitHub lifecycle dates; events are local observations'}
    if repo:
        empty['repo'] = repo
    try:
        db = connect(Path(state).absolute())
    except sqlite3.Error:
        return {**empty, 'errors': [{'error': 'Local history database unavailable'}]}
    if db is None:
        return empty
    try:
        if repo:
            condition, args = 'repo=?', (repo,)
        elif repos is not None:
            condition, args = 'repo IN (' + ','.join('?' for _ in repos) + ')', tuple(repos)
        else:
            condition, args = '', ()
        where = ' WHERE ' + condition if condition else ''
        repositories = [dict(row) for row in db.execute('SELECT * FROM repositories' + where, args)]
        requested = {repo} if repo else set(repos or [])
        missing = sorted(requested - {row['repo'] for row in repositories})
        items = [{**json.loads(row['data']), 'evidence_observed_at': row['evidence_updated_at']} for row in db.execute('SELECT data,evidence_updated_at FROM items' + where, args)]
        for item in items:
            for evidence in item.get('evidence', []):
                classify_evidence(evidence)
        events = [dict(row) for row in db.execute('SELECT * FROM events WHERE observed_at>=?' + (' AND ' + condition if condition else '') + ' ORDER BY observed_at DESC,id DESC LIMIT 1000', (cutoff(days),) + args)]
        start = cutoff(days)[:10]
        start_date = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        daily = {(start_date + timedelta(days=i)).isoformat(): {'date': (start_date + timedelta(days=i)).isoformat(), 'opened': 0, 'completed': 0, 'merged': 0} for i in range(days)}
        start = start_date.isoformat()
        for item in items:
            for date, metric in ((item.get('created_at'), 'opened'),
                                 (item.get('closed_at') if item['kind'] == 'issue' and item['stage'] == 'completed' else None, 'completed'),
                                 (item.get('merged_at'), 'merged')):
                if date and date[:10] in daily:
                    bucket = daily.setdefault(date[:10], {'date': date[:10], 'opened': 0, 'completed': 0, 'merged': 0})
                    if metric != 'opened' or item['kind'] == 'issue':
                        bucket[metric] += 1
        for row in repositories:
            row['pending_evidence'] = db.execute('SELECT count(*) FROM items WHERE repo=? AND ' + EVIDENCE_DUE, (row['repo'], evidence_cutoff())).fetchone()[0]
            if row['pending_evidence'] and row['status'] == 'ok':
                row['status'] = 'partial'
            row['missing_evidence'] = db.execute('SELECT count(*) FROM items WHERE repo=? AND evidence_updated_at IS NULL', (row['repo'],)).fetchone()[0]
            row['refresh_due'] = row['pending_evidence'] - row['missing_evidence']
            row['counts_by_kind'] = {kind: {stage: sum(x['repo'] == row['repo'] and x['kind'] == kind and x['stage'] == stage for x in items) for stage in STAGES} for kind in ('issue', 'pr')}
            row['counts'] = {stage: sum(x['repo'] == row['repo'] and x['stage'] == stage for x in items) for stage in STAGES}
        dates = [row['observed_at'] for row in repositories if row['observed_at']]
        coverages = [row['coverage_since'] for row in repositories if row['coverage_since']]
        for day in daily.values():
            day['within_coverage'] = bool(coverages) and day['date'] >= max(coverages)[:10]
        return {**empty, 'status': ('partial' if missing or any(row['status'] != 'ok' for row in repositories) else 'ok') if repositories else 'unavailable',
            'observed_at': min(dates) if dates else None, 'coverage_since': max(coverages) if coverages else None,
            'errors': [{'repo': row['repo'], 'error': row['error']} for row in repositories if row['error']] + [{'repo': value, 'error': 'No local collection yet'} for value in missing],
            'repos': repositories, 'items': items if repo else [], 'events': events,
            'daily': [daily[key] for key in sorted(daily)],
            'counts': {stage: sum(x['stage'] == stage for x in items) for stage in STAGES},
            'pending_evidence': sum(row['pending_evidence'] for row in repositories),
            'missing_evidence': sum(row['missing_evidence'] for row in repositories),
            'refresh_due': sum(row['refresh_due'] for row in repositories),
            'counts_by_kind': {kind: {stage: sum(x['kind'] == kind and x['stage'] == stage for x in items) for stage in STAGES} for kind in ('issue', 'pr')},
            'item_count': len(items)}
    except (sqlite3.Error, ValueError, KeyError):
        return {**empty, 'errors': [{'error': 'Local history database unavailable'}]}
    finally:
        db.close()


def read_overview(state, days=30, repos=None):
    return _read(state, days=days, repos=repos)


def read_repository(state, repo, days=30):
    return _read(state, repo=repo, days=days)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--config')
    parser.add_argument('--repo', action='append', default=[])
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--evidence-limit', type=int, default=8)
    args = parser.parse_args()
    repos = args.repo
    if args.config:
        from .config import load_repositories
        repos += [r.repo for r in load_repositories(args.config) if r.enabled]
    if not repos:
        parser.error('--repo or --config is required')
    print(json.dumps(collect(args.state, sorted(set(repos)), args.days, args.evidence_limit)))


if __name__ == '__main__':
    main()
