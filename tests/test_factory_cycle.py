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
    def inspect(self, job, *, resources=False): return self.jobs.get(job['id'])
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
    def ci_evidence(self, job): return None
    def publisher(self, role): return role + '[bot]'
    def pulls(self, item): return [self.pull] if self.pull else []
    def own_draft(self, item, number, publisher): return getattr(self, 'draft', None)
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
    def settle_checks(self, item, candidate, summary): self.settled = getattr(self, 'settled', []) + [candidate['key']]; return 1
    def verdict(self, job, report): return self.check(job, report)
    def human(self, *args): return self.approved, False
    def approvals(self, *args):
        return [{'login': 'maintainer', 'state': 'APPROVED', 'at': 1, 'counted': True, 'why': None}] if self.approved else []
    def gates(self, item): return set(item['evidence']) >= {'review', 'audit'}
    def protections(self, *args): return True
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
        report = {'version': 1, 'attempt': job['id'], 'role': role, 'outcome': outcome, 'summary': 'Verified findings',
                  'headline': 'The step finished', 'plain': 'The worker finished this step. Forge moves the issue on.'}
        if role in ('review', 'audit'): report['candidate'] = job['candidate']['key']
        if role == 'audit': report.update(human_required=human, human_reason='Policy reason')
        if role == 'recovery': report.update(pattern='logic', evidence=['check failure'], uncertainty='', alternative=None)
        self.engine.finish(job, report); return job
    def state(self): return self.ledger.read()['issues'][self.key]['state']

    def test_resume_owned_draft_records_the_choice_without_accepting_it(self):
        self.github.draft = {'pr': 22, 'branch': 'forge/issue-1-existing', 'superseded': [21]}
        self.cycle()
        job = self.active()
        self.assertEqual(job['branch'], self.github.draft['branch'])
        self.assertEqual(job['resume_branch'], job['branch'])
        from forge.factory.contracts import assignment_note
        self.assertEqual(assignment_note(job)['resume_pr'], 22)
        self.assertIsNone(job['candidate'])
        event = next(e for e in self.ledger.read()['events'] if e['kind'] == 'draft_resumed')
        self.assertEqual(event['pr'], 22)
        self.assertEqual(event['superseded'], [21])

    def test_decision_survives_restart_without_pr_failure_or_review_and_answer_reaches_build(self):
        self.cycle()
        build = self.active()
        decision = {'question': 'Accept the residual advisory?', 'options': ['Accept an exception', 'Wait for a fix'],
                    'recommendation': 'Wait for a fix', 'evidence': 'No supported release fixes the advisory.'}
        self.engine.finish(build, {'version': 1, 'attempt': build['id'], 'role': 'build',
                                  'outcome': 'needs_decision', 'summary': 'A policy decision is needed.', 'decision': decision})
        self.assertFalse(self.cycle()['errors'])
        self.assertFalse(self.cycle()['errors'])
        data = self.ledger.read()
        self.assertEqual(self.state(), 'hold')
        self.assertEqual(data['issues'][self.key]['failures'], [])
        self.assertEqual(data['issues'][self.key]['decision_request']['question'], decision['question'])
        self.assertEqual(len(data['jobs']), 1)
        self.assertFalse(data['slots'])
        self.assertIsNone(self.github.pull)
        self.assertFalse(self.github.checks)
        self.ledger.retry(self.key, 'Use the now available compatible release')
        self.assertFalse(self.cycle()['errors'])
        next_job = self.active()
        self.assertEqual(next_job['role'], 'build')
        self.assertEqual(next_job['history']['retry_plan'], 'Use the now available compatible release')
        self.assertEqual(next_job['branch'], build['branch'])
        self.assertNotIn('decision_request', self.ledger.read()['issues'][self.key])
        self.assertTrue(any(e['kind'] == 'decision_answered' for e in self.ledger.read()['events']))

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

    def test_a_conflict_while_waiting_for_approval_returns_to_build_without_charging_an_attempt(self):
        self.cycle(); build = self.active(); self.github.publish(build); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required'); self.cycle()
        self.assertEqual(self.state(), 'approval_needed')
        # The target moves on and the PR now conflicts; nobody has approved yet.
        self.github.pull['mergeable'] = False
        self.cycle()
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['failures'], [])
        self.assertIn('Update the existing branch', item['retry_plan'])
        self.assertEqual(item['evidence'], {})
        self.assertEqual(self.active()['role'], 'build')
        self.assertEqual(self.active()['branch'], build['branch'])

    def test_a_conflict_check_that_cannot_be_published_leaves_the_issue_to_retry(self):
        from forge.factory.github import GitHubUnavailable
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required'); self.cycle()
        self.github.pull['mergeable'] = False
        publish = self.github.check
        self.github.check = lambda *a, **k: (_ for _ in ()).throw(GitHubUnavailable('Checks API unavailable'))
        self.cycle()
        self.assertEqual(self.state(), 'approval_needed')
        self.github.check = publish
        self.cycle()
        self.assertEqual(self.active()['role'], 'build')

    def test_a_conflict_found_after_approval_also_returns_to_build_uncharged(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle()
        self.github.pull['mergeable'] = False
        self.finish('audit'); self.cycle()
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(self.ledger.failure_count(item), 0)
        self.assertEqual(self.active()['role'], 'build')

    def test_human_wait_then_approval_without_additional_audit(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required'); self.cycle()
        self.assertEqual(self.state(), 'approval_needed'); self.assertFalse(self.ledger.read()['slots'])
        for _ in range(3): self.cycle()
        self.assertEqual(len(self.ledger.read()['jobs']), 3)
        self.github.approved = True; self.cycle(); self.assertEqual(self.state(), 'merged')

    def test_merge_refusal_is_bound_to_candidate_and_clears_after_retry(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit')
        merge = self.github.merge
        def refuse(item):
            raise GitHubUnavailable('GitHub refused to merge: branch rules still block it', status=405)
        self.github.merge = refuse
        self.assertTrue(self.cycle()['errors'])
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['merge_gate']['kind'], 'refused')
        self.assertEqual(item['merge_gate']['candidate'], item['candidate']['key'])
        self.github.merge = merge
        self.cycle()
        self.assertEqual(self.state(), 'merged')
        self.assertEqual(self.ledger.read()['issues'][self.key]['merge_gate']['kind'], 'ready')

    def test_a_refused_merge_does_not_pin_a_later_conflict(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit')
        def refuse(item):
            raise GitHubUnavailable('GitHub refused to merge: branch rules still block it', status=405)
        self.github.merge = refuse
        self.cycle()
        merge_job = self.ledger.read()['issues'][self.key]['active']
        self.assertEqual(self.ledger.read()['jobs'][merge_job]['role'], 'merge')
        self.github.pull['mergeable'] = False
        self.cycle()
        data = self.ledger.read()
        self.assertEqual(data['jobs'][merge_job]['outcome'], 'superseded')
        self.assertEqual(self.active()['role'], 'build')
        self.assertEqual(self.ledger.failure_count(data['issues'][self.key]), 0)

    def test_technical_gate_is_not_a_missing_human_approval(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required')
        self.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['merge_gate']['kind'], 'approval')
        self.github.approved = True
        self.github.gates = lambda item: False
        self.cycle()
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['merge_gate']['kind'], 'blocked')
        self.assertIn('review and audit checks', item['merge_gate']['reason'])
        self.assertEqual(item['merge_gate']['approvals'][0]['login'], 'maintainer')

    def test_an_escalated_change_merges_on_a_counted_approval_without_codeowners(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required')
        self.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['merge_gate']['approvals'], [])
        self.github.approved = True
        self.cycle()
        self.assertTrue(self.github.pull['merged'])

    def test_a_check_request_is_observed_first_and_then_cleared(self):
        from forge.state import AtomicStore
        from forge.factory.control import request_check
        self.cycle()
        with AtomicStore(self.temp.name, 'check-requests').transaction() as data:
            data[self.key] = 5.0
        self.cycle()
        self.assertEqual(AtomicStore(self.temp.name, 'check-requests').read(), {})
        with self.assertRaises(ValueError):
            request_check(self.temp.name + '/host', 'unknown#1')

    def test_entering_approval_is_recorded_once(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required')
        self.cycle(); self.cycle()
        changes = [e for e in self.ledger.read()['events'] if e['kind'] == 'state_changed']
        self.assertEqual([(e['previous'], e['state']) for e in changes], [('audited', 'approval_needed')])

    def test_approval_priority_cannot_starve_the_rotation(self):
        from forge.factory.supervisor import Supervisor
        self.assertGreater(Supervisor.PRIORITY_OBSERVE_SECONDS, 2 * 84)  # longer than two measured cycles

    def test_a_stopped_candidate_settles_its_checks_once(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        with self.ledger.transaction() as data:
            data['issues'][self.key]['active'] = None
        self.ledger.hold(self.key, 'Paused by an operator')
        self.cycle(); self.cycle()
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(getattr(self.github, 'settled', []), [item['candidate']['key']])
        self.assertEqual(item['checks_settled'], item['candidate']['key'])

    def test_an_unreadable_pull_request_preserves_open_work(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required'); self.cycle()
        def gone(item, number): raise GitHubUnavailable('GitHub observation or publication is unconfirmed', status=404)
        before = self.ledger.read()['issues'][self.key]
        self.github.pr = gone
        self.assertTrue(self.cycle()['errors'])
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual((item['state'], item['candidate'], item['evidence']),
                         (before['state'], before['candidate'], before['evidence']))

    def test_a_closed_issue_whose_pull_request_is_gone_is_cancelled(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit', human='required'); self.cycle()
        def gone(item, number): raise GitHubUnavailable('GitHub observation or publication is unconfirmed', status=404)
        self.github.pr = gone; self.github.closed = True
        self.assertFalse(self.cycle()['errors'])
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['state'], 'cancelled')
        self.assertEqual(item['github_observation']['issue']['state'], 'closed')
        self.assertIsNone(item['github_observation']['issue']['state_reason'])
        self.assertIsNone(item['candidate'])
        self.assertFalse(self.cycle()['errors'])

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
