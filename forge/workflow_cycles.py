"""Durable issue results and retry budget. All writes use the engine transaction.

Only trusted, revision-bound published-result verifiers call record_result.
Labels, process exits, release receipts and provider retries are not failures.
"""
import json

from . import workflow_definition as policy

SCHEMA = """
CREATE TABLE IF NOT EXISTS issue_retry_state(
 repo TEXT NOT NULL, number INTEGER NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
 rejected_cycles INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
 reset_at REAL, last_result TEXT, PRIMARY KEY(repo,number));
CREATE TABLE IF NOT EXISTS issue_results(
 assignment_id TEXT PRIMARY KEY, repo TEXT NOT NULL, role TEXT NOT NULL,
 outcome TEXT NOT NULL, result_id TEXT NOT NULL, evidence TEXT NOT NULL,
 recorded_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rejected_build_cycles(
 repo TEXT NOT NULL, number INTEGER NOT NULL, epoch INTEGER NOT NULL,
 cycle_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
 PRIMARY KEY(repo,number,epoch,cycle_id));
"""


def retry_state(db, repo, number):
    row = db.execute('SELECT * FROM issue_retry_state WHERE repo=? AND number=?', (repo, number)).fetchone()
    return dict(row) if row else dict(repo=repo, number=number, epoch=0, rejected_cycles=0,
                                     failed=0, reset_at=None, last_result=None)


def project(db, repo, item):
    retry = retry_state(db, repo, item['number'])
    if retry['failed'] and item['stage'] not in policy.TERMINAL:
        item.update(stage='failed', disposition='stalled', reason='rejected_cycle_limit')
    return item


def authorize_dispatch(db, repo, role, pr):
    if not pr or role not in {'build','audit'}:
        return
    attempted_roles = ('review',) if role == 'audit' else ('review','audit')
    attempts = []
    for row in db.execute("SELECT id,role,pr,created_at FROM assignments WHERE repo=? AND submitted_at IS NOT NULL ORDER BY created_at DESC,rowid DESC", (repo,)):
        candidate = json.loads(row['pr']) if row['pr'] else None
        if row['role'] in attempted_roles and candidate and candidate['number'] == pr['number']:
            attempts.append((row,candidate))
    if not attempts:
        return  # Existing untracked pipeline: no retrospective execution invented.
    matching = next(((a,p) for a,p in attempts if p == pr), None)
    if matching is None:
        if role == 'audit':
            raise ValueError('current_revision_requires_review')
        return
    result = db.execute('SELECT outcome FROM issue_results WHERE assignment_id=?', (matching[0]['id'],)).fetchone()
    expected = 'pass' if role == 'audit' else 'fail'
    if not result or result['outcome'] != expected:
        raise ValueError('verified_review_pass_required' if role == 'audit' else 'verified_rejection_required')


def record(db, assignment, evidence, now):
    repo = assignment['repo']; ident = assignment['id']
    assigned_row = db.execute('SELECT rowid FROM assignments WHERE id=?', (ident,)).fetchone()[0]
    db.execute('INSERT INTO issue_results VALUES(?,?,?,?,?,?,?)',
               (ident, repo, assignment['role'], evidence['outcome'], evidence['result_id'],
                json.dumps(evidence, sort_keys=True), now))
    results = []
    for number in assignment['issue_numbers']:
        row = db.execute('SELECT * FROM issues WHERE repo=? AND number=?', (repo, number)).fetchone()
        if row is None:
            raise ValueError('result_issue_missing')
        retry = retry_state(db, repo, number)
        # A late receipt from before an explicit retry cannot consume its new budget.
        before_reset = retry['reset_at'] is not None and assignment['created_at'] <= retry['reset_at']
        if evidence['outcome'] == 'fail' and not before_reset:
            builds = db.execute("SELECT id,issue_numbers FROM assignments WHERE repo=? AND role='build' "
                                "AND submitted_at IS NOT NULL AND (created_at<? OR (created_at=? AND rowid<?)) "
                                "ORDER BY created_at DESC,rowid DESC",
                                (repo, assignment['created_at'], assignment['created_at'], assigned_row))
            builder = next((b for b in builds if number in json.loads(b['issue_numbers'])), None)
            # Legacy initial candidates have no observed builder; use exact revision.
            cycle = builder['id'] if builder else 'candidate:' + json.dumps(assignment['pr'], sort_keys=True)
            inserted = db.execute('INSERT OR IGNORE INTO rejected_build_cycles VALUES(?,?,?,?,?)',
                                  (repo, number, retry['epoch'], cycle, ident)).rowcount
            retry['rejected_cycles'] += inserted
            if retry['rejected_cycles'] >= policy.MAX_REJECTED_CYCLES and row['stage'] not in policy.TERMINAL:
                retry['failed'] = 1
        retry['last_result'] = ident
        db.execute('INSERT INTO issue_retry_state VALUES(?,?,?,?,?,?,?) ON CONFLICT(repo,number) DO UPDATE SET '
                   'rejected_cycles=excluded.rejected_cycles,failed=excluded.failed,last_result=excluded.last_result',
                   tuple(retry[k] for k in ('repo','number','epoch','rejected_cycles','failed','reset_at','last_result')))
        target = policy.next_stage(assignment['role'], evidence['outcome'], rejected_cycles=retry['rejected_cycles'])
        # Historical result recording must not rewind a newer candidate or completed issue.
        links = [dict(p) for p in db.execute('SELECT number,head,base FROM pr_links WHERE repo=? AND issue_number=?', (repo,number))]
        current = assignment['pr'] in links
        if row['stage'] not in policy.TERMINAL and not before_reset:
            if retry['failed']:
                db.execute("UPDATE issues SET stage='failed',disposition='stalled',reason='rejected_cycle_limit' WHERE repo=? AND number=?", (repo,number))
            elif current and row['stage'] in {assignment['role'], target}:
                # Hold and human approval are orthogonal gates; never clear them.
                disposition = row['disposition'] if row['disposition'] in {'critical','stalled'} else 'pending'
                db.execute('UPDATE issues SET stage=?,disposition=? WHERE repo=? AND number=?', (target,disposition,repo,number))
        results.append(dict(number=number, rejected_cycles=retry['rejected_cycles'], failed=bool(retry['failed']),
                            transition=target, historical=not current or before_reset))
    return results
