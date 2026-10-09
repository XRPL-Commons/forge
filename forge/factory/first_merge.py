"""Bounded evidence for a pilot completed through the normal Forge pipeline."""
import math
import time

from ..store.runtime import selected
from ..store.queries import Queries
from .status_projection import operator_status


def proof(root, *, repository, number, started_at, clock=time.time):
    if (not isinstance(repository, dict) or type(repository.get('id')) is not int or repository['id'] <= 0
            or not isinstance(repository.get('name'), str) or type(number) is not int or number <= 0
            or type(started_at) not in (int, float) or not math.isfinite(started_at) or started_at <= 0):
        raise ValueError('Select the repository, pilot issue and saved installation start time')
    now = clock()
    if started_at > now:
        raise ValueError('Installation start time is in the future')
    result = dict(status='waiting', observed_at=now, repository=repository, number=number,
                  candidate=None, evidence=None, attempts={'total':0,'charged_failures':0})
    store = selected(root)
    if store is None:
        return result
    with store.read_snapshot() as db:
        repo = db.execute('SELECT full_name FROM repositories WHERE id=?', (repository['id'],)).fetchone()
        if repo is None:
            return result
        if repo[0] != repository['name']:
            raise ValueError('Repository identity changed')
        issue = db.execute('SELECT id FROM issues WHERE repository_id=? AND github_number=?', (repository['id'],number)).fetchone()
        if issue is None:
            return result
        projected = Queries._issue(db, issue[0], include_body=False)
        result['operator_status'] = operator_status(projected)
        life = projected['lifecycle']
        unit_id = life['unit_id'] or life['historical_unit_id']
        if not unit_id:
            return result
        result['unit_id'] = unit_id
        result['attempts'] = dict(total=db.execute('SELECT COUNT(*) FROM attempts WHERE unit_id=?',(unit_id,)).fetchone()[0],
            charged_failures=db.execute("SELECT COALESCE(SUM(CASE b.kind WHEN 'charge' THEN 1 ELSE -1 END),0) FROM budget_entries b JOIN attempts a ON a.id=b.attempt_id WHERE a.unit_id=?",(unit_id,)).fetchone()[0])
        unit = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
        candidate = db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
        if candidate is None:
            return result
        result['candidate'] = dict(id=candidate['id'], pull_number=candidate['pull_number'],
            head_sha=candidate['head_oid'], base_sha=candidate['base_oid'], merge_sha=unit['merge_sha'])
        if unit['state'] != 'merged' or unit['created_at'] < started_at or candidate['accepted_at'] < started_at:
            return result
        roles = {}
        for role, kind in (('build','publication'),('review','review'),('audit','audit')):
            row = db.execute("SELECT j.id FROM candidate_evidence e JOIN jobs j ON j.attempt_id=e.attempt_id AND j.unit_id=e.unit_id "
                "JOIN job_launches l ON l.job_id=j.id WHERE e.candidate_id=? AND e.kind=? AND e.verdict='pass' "
                "AND j.role=? AND j.outcome='success' AND j.cleanup=1 AND j.created_at>=? AND e.observed_at>=? "
                'ORDER BY e.observed_at DESC,e.id DESC LIMIT 1',
                (candidate['id'],kind,role,started_at,started_at)).fetchone()
            if row is None:
                return result
            roles[role] = row[0]
        effect = db.execute("SELECT e.id,e.updated_at,m.merge_oid FROM merge_requests m "
            "JOIN external_effects e ON e.id=m.effect_id JOIN candidate_effects c ON c.effect_id=e.id AND c.candidate_id=m.candidate_id "
            "WHERE m.unit_id=? AND m.candidate_id=? AND m.status='merged' AND e.kind='github_merge' "
            "AND e.status='confirmed' AND e.created_at>=? AND e.updated_at>=? ORDER BY e.updated_at DESC LIMIT 1",
            (unit_id,candidate['id'],started_at,started_at)).fetchone()
        if effect is None or not effect['merge_oid'] or effect['merge_oid'] != unit['merge_sha']:
            return result
        result['status'] = 'merged'
        result['evidence'] = dict(merge_effect_id=effect['id'],confirmed_at=effect['updated_at'],role_jobs=roles,
            approval_hold_observed=bool(db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND candidate_id=? AND code='approval_required' AND resolved_at IS NOT NULL LIMIT 1",(unit_id,candidate['id'])).fetchone()))
    return result
