"""Contract-level scenarios across restarts, using independent engine/GitHub fakes."""
import copy
import json
import tempfile
import unittest

from forge.factory.config import validate
from forge.factory.contracts import candidate
from forge.factory.github import GitHubUnavailable
from forge.factory.ledger import Ledger
from forge.factory.supervisor import Supervisor


class Engine:
    def __init__(self):
        self.jobs = {}
        self.reset_ids = []
    def capacity(self, job=None): return {'allowed': True, 'reason': 'capacity_available'}
    def ready(self): return True
    def create(self, job):
        self.jobs[job['id']] = {'status': 'created', 'running': False, 'result': None}
        return job['id']
    def inspect(self, job): return self.jobs.get(job['id'])
    def prepare(self, job, token): pass
    def start(self, job): self.jobs[job['id']].update(status='running', running=True)
    def observe(self, job, current=None): return {'logs': 'running output', 'at': 1}
    def capture(self, job): return {'result': self.jobs[job['id']]['result'], 'logs': 'retained evidence', 'missing': []}
    def reset(self, job):
        assert job['fenced']
        self.reset_ids.append(job['id']); self.jobs.pop(job['id'], None)
        return {'container_absent': True, 'capacity_ready': True}
    def finish(self, job, report=None): self.jobs[job['id']].update(status='exited', running=False, result=report)


class GitHubFacts:
    def __init__(self):
        self.pull = None
        self.checks = []
        self.available = True
        self.approved = False
        self.closed = False
    def publisher(self, role): return role + '[bot]'
    def target(self, item): return 'main'
    def token(self, *args, **kwargs): return 'artificial-token'
    def issue(self, item):
        if not self.available: raise GitHubUnavailable('GitHub unavailable')
        return {'state': 'closed' if self.closed else 'open', 'labels': [], 'title': 'Implement behavior'}, 'requirements'
    def publication(self, job, requirements, policy):
        if not self.pull: return None
        value = candidate(self.pull, job, requirements, policy)
        value['branch'] = self.pull['head']['ref']
        return value
    def pr(self, item, number): return copy.deepcopy(self.pull)
    def check(self, *args): self.checks.append(args); return len(self.checks)
    def verdict(self, job, report): return self.check(job, report)
    def human(self, *args): return self.approved, False
    def gates(self, item): return set(item['evidence']) >= {'review', 'audit'}
    def protections(self, *args): return True
    def native_human_gate(self, *args): return True
    def merge(self, item): self.pull['merged'] = True; self.pull['merge_commit_sha'] = 'c'*40
    def close_merged_issue(self, item, issue):
        assert self.pull['merged']
        self.closed = True
    def labels(self, item): pass
    def publish(self, job):
        meta = {'version': 1, 'issue': 1, 'attempt': job['id'], 'head': 'a'*40}
        self.pull = {'number': 2, 'body': '<!-- forge-submission '+json.dumps(meta)+' -->',
                     'head': {'sha': 'a'*40, 'ref': job['branch'], 'repo': {'id': 1}},
                     'base': {'sha': 'b'*40, 'ref': 'main', 'repo': {'id': 1}},
                     'user': {'login': 'build[bot]', 'type': 'Bot'}, 'draft': False,
                     'state': 'open', 'merged': False, 'mergeable': True}


class Settings:
    def admission(self, repo): return {'allowed': True}
    def view(self): return {'repositories': []}


class CycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(self.temp.name)
        self.key = self.ledger.discover({'id': 1, 'name': 'example/project'}, 1, 'requirements')
        self.config = validate({'enabled': True, 'merge_enabled': True, 'image': 'sha256:'+'a'*64,
                                'provider_environment': [], 'harness_argv': ['/adapter']})
        self.engine, self.github = Engine(), GitHubFacts()
    def cycle(self):
        # Recreate the supervisor and ledger every cycle: no in-memory authority.
        controller = Supervisor(Ledger(self.temp.name), self.engine, self.github, Settings(), self.config)
        return controller.cycle()
    def active(self):
        jobs = [j for j in self.ledger.read()['jobs'].values() if not j['cleanup']]
        self.assertEqual(len(jobs), 1)
        return jobs[0]
    def finish(self, role, outcome='pass', human='not_required'):
        job = self.active(); self.assertEqual(job['role'], role)
        report = {'version': 1, 'attempt': job['id'], 'role': role, 'outcome': outcome, 'summary': 'Verified findings'}
        if role in ('review', 'audit'): report['candidate'] = job['candidate']['key']
        if role == 'audit': report.update(human_required=human, human_reason='Policy reason')
        if role == 'recovery': report.update(pattern='logic', evidence=['check failure'], uncertainty='', alternative=None)
        self.engine.finish(job, report); return job
    def state(self): return self.ledger.read()['issues'][self.key]['state']

    def test_build_review_audit_merge_with_restart_at_every_step(self):
        self.assertFalse(self.cycle()['errors']); build = self.active()
        self.github.publish(build); self.finish('build')
        self.assertFalse(self.cycle()['errors']); self.finish('review')
        self.assertFalse(self.cycle()['errors']); self.finish('audit')
        self.assertFalse(self.cycle()['errors']); self.assertEqual(self.state(), 'merged')
        self.assertFalse(self.ledger.read()['slots'])
        self.assertEqual(len(self.engine.reset_ids), 3)
        self.cycle(); self.assertEqual(len(self.ledger.read()['jobs']), 4)
        self.assertTrue(self.github.closed)

    def test_audit_rejection_investigation_rebuild_full_pipeline(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', 'reject'); self.cycle()
        self.assertEqual(self.state(), 'investigating'); self.finish('recovery'); self.cycle()
        self.assertEqual(self.active()['role'], 'build')
        self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit'); self.cycle()
        self.assertEqual(self.state(), 'merged')
        self.assertEqual(self.ledger.failure_count(self.ledger.read()['issues'][self.key]), 1)

    def test_unknown_github_outcome_frees_runner_but_prevents_duplicate_build(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.github.available = False
        self.assertTrue(self.cycle()['errors']); self.assertFalse(self.ledger.read()['slots'])
        self.assertEqual(len(self.ledger.read()['jobs']), 1)
        self.github.available = True; self.cycle()
        self.assertEqual(self.active()['role'], 'review')

    def test_human_wait_then_approval_without_additional_audit(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required'); self.cycle()
        self.assertEqual(self.state(), 'approval_needed'); self.assertFalse(self.ledger.read()['slots'])
        for _ in range(3): self.cycle()
        self.assertEqual(len(self.ledger.read()['jobs']), 3)
        self.github.approved = True; self.cycle(); self.assertEqual(self.state(), 'merged')

    def test_external_merge_clears_an_old_failure_alert(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review', 'reject'); self.cycle()
        self.github.pull['merged'] = True
        self.cycle()
        self.assertEqual(self.state(), 'merged')
        self.assertEqual(self.ledger.failure_count(self.ledger.read()['issues'][self.key]), 1)

    def test_developer_edit_pauses_assignment_without_overwriting_or_failure(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle()
        self.github.pull['head']['sha'] = 'd'*40
        self.cycle(); self.cycle()
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['state'], 'hold')
        self.assertFalse(item['evidence'])
        self.assertFalse(self.ledger.read()['slots'])
        self.assertEqual(self.github.pull['head']['sha'], 'd'*40)
        self.assertEqual(self.ledger.failure_count(item), 0)
        self.assertEqual(len(self.ledger.read()['jobs']), 3)

    def test_developer_edit_during_completed_audit_does_not_dispatch_rebuild(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit')
        self.github.pull['head']['sha'] = 'd'*40
        self.cycle()
        self.assertEqual(self.state(), 'hold')
        self.assertFalse(self.github.pull['merged'])
        self.assertFalse(self.ledger.read()['slots'])

    def test_developer_push_racing_verdict_publication_is_held(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        original = self.github.verdict
        def verdict(job, report):
            result = original(job, report)
            self.github.pull['head']['sha'] = 'd'*40
            return result
        self.github.verdict = verdict
        self.finish('review'); self.cycle()
        self.assertEqual(self.state(), 'hold')
        self.assertFalse(self.ledger.read()['issues'][self.key]['evidence'])
        self.assertEqual(len(self.ledger.read()['jobs']), 2)

    def test_human_merge_during_audit_is_observed_and_worker_is_cleaned_up(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle()
        self.github.pull.update(merged=True, merge_commit_sha='c'*40)
        self.cycle(); self.cycle()
        self.assertEqual(self.state(), 'merged')
        self.assertFalse(self.ledger.read()['slots'])
        self.assertEqual(len(self.ledger.read()['jobs']), 3)

    def test_base_change_invalidates_evidence_without_failure(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required'); self.cycle()
        self.github.pull['base']['sha'] = 'd'*40; self.cycle()
        self.assertEqual(self.active()['role'], 'review')
        self.assertEqual(self.ledger.failure_count(self.ledger.read()['issues'][self.key]), 0)


if __name__ == '__main__': unittest.main()
