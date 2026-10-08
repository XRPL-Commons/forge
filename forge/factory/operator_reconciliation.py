"""Fresh read-only evidence for imported operator intent interruption."""
import json
import time
from ..store.operator_reconciliation import OperatorReconciliation
from ..store.reconciliation import authorization_in
from .github import GitHubUnavailable


class ImportedOperatorController:
    def __init__(self,store,github,*,clock=time.time):
        self.store,self.github,self.clock=store,github,clock
    def step(self,issue_id):
        with self.store.read_snapshot() as db:
            if not authorization_in(db):return {'pending':'migration_authorization_required'}
            pending=db.execute("SELECT 1 FROM issue_action_requests q JOIN imported_operator_requests p ON p.request_id=q.id LEFT JOIN operator_action_authority a ON a.request_id=q.id WHERE q.issue_id=? AND q.status='pending' AND a.request_id IS NULL",(issue_id,)).fetchone()
            sync=db.execute("SELECT 1 FROM unit_gates g JOIN unit_issues m ON m.unit_id=g.unit_id WHERE m.issue_id=? AND m.left_at IS NULL AND g.code='operator_sync' AND g.resolved_at IS NULL AND json_extract(g.details_json,'$.legacy_field')='operator_sync'",(issue_id,)).fetchone()
            if not pending and not sync:return {'interrupted':False}
            issue=db.execute('SELECT i.*,r.full_name FROM issues i JOIN repositories r ON r.id=i.repository_id WHERE i.id=?',(issue_id,)).fetchone()
        item=dict(repo_id=issue['repository_id'],repo=issue['full_name'],number=issue['github_number'])
        try:remote,_=self.github.issue(item)
        except GitHubUnavailable:return {'pending':'github_observation'}
        if remote.get('number')!=issue['github_number'] or 'pull_request' in remote:raise GitHubUnavailable('Imported decision issue identity is unverified')
        observed={key:remote.get(key) for key in ('number','state','state_reason','updated_at','labels','assignees')}
        return OperatorReconciliation(self.store,clock=self.clock).interrupt(issue_id,proof={'verified':True,'observed_at':self.clock(),'issue_version':issue['version'],'issue':observed})
