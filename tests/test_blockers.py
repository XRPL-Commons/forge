import tempfile
import unittest
from forge.activity import record
from forge.blockers import read_blockers


class BlockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rows=[{'repo':'o/r','short':'r','audit':{'up':False}}]

    def test_cooldown_explains_retry_without_promising_progress(self):
        value=read_blockers(self.tmp.name,self.rows,{'sessions':{'r-audit':{'until':3700,'reason':'no_progress'}}},{},now=200)['r-audit']
        self.assertEqual(value['remaining_seconds'],3500)
        self.assertIn('underlying cause is not recorded',value['why'])
        self.assertIn('will not clear',value['next'])

    def test_role_limit_uses_matching_durable_receipt(self):
        record(self.tmp.name,'admission_deferred','r-audit',metadata={'capacity':{'reason':'role_session_limit','observed_at':100,'role_counts':{'audit':2},'limits':{'max_sessions_per_role':2}}},now=100)
        value=read_blockers(self.tmp.name,self.rows,{}, {},now=200)['r-audit']
        self.assertIn('2 audit sessions',value['why'])
        self.assertFalse(value['stale'])
        self.assertTrue(read_blockers(self.tmp.name,self.rows,{}, {},now=1100)['r-audit']['stale'])
        record(self.tmp.name,'dispatch','r-audit',now=1200)
        self.assertEqual(read_blockers(self.tmp.name,self.rows,{}, {},now=1210),{})

    def test_live_session_and_private_session_do_not_inherit_denial(self):
        record(self.tmp.name,'admission_deferred','private',now=100)
        self.assertEqual(read_blockers(self.tmp.name,self.rows,{}, {},now=200),{})
        record(self.tmp.name,'admission_deferred','r-audit',now=100)
        self.rows[0]['audit']['up']=True
        self.assertEqual(read_blockers(self.tmp.name,self.rows,{}, {},now=200),{})

    def test_current_runtime_failure_has_actionable_cause(self):
        self.rows[0]['audit'] = {'up': True, 'busy': False, 'state':'blocked', 'provider':'codex'}
        observation = {'observed_at': 200, 'sessions': {'r-audit': {
            'provider':'codex', 'state':'blocked', 'reason':'runtime_unavailable'}}}
        value = read_blockers(self.tmp.name,self.rows,{},observation,now=220)['r-audit']
        self.assertIn('tool-execution host', value['why'])
        self.assertIn('codex-code-mode-host', value['next'])
        self.assertEqual(read_blockers(self.tmp.name,self.rows,{},observation,now=300),{})
        self.rows[0]['audit']['busy'] = True
        self.assertEqual(read_blockers(self.tmp.name,self.rows,{},observation,now=220),{})

    def test_expired_guard_is_not_current_blocker(self):
        self.assertEqual(read_blockers(self.tmp.name,self.rows,{'sessions':{'r-audit':{'until':100,'reason':'no_progress'}}},{},now=200),{})

    def test_builder_pause_explains_builder_work_without_audit_claims(self):
        value=read_blockers(self.tmp.name,self.rows,{'sessions':{'r-build':{'until':3700,'reason':'no_progress'}}},{},now=200)['r-build']
        self.assertIn('Builder retries',value['why'])
        self.assertIn('Open the builder output',value['next'])
        self.assertNotIn('audit',value['why']+value['next'])
        self.assertNotIn('nudge budget',value['why'])
        self.assertNotIn('evidence',value)

    def test_reviewer_pause_directs_operator_to_review_output(self):
        value=read_blockers(self.tmp.name,self.rows,{'sessions':{'r-review':{'until':3700,'reason':'no_progress'}}},{},now=200)['r-review']
        self.assertIn('Reviewer retries',value['why'])
        self.assertIn('Open the reviewer output',value['next'])
        self.assertNotIn('audit',value['why']+value['next'])

    def test_matching_pause_receipt_provides_observation_without_inventing_cause(self):
        record(self.tmp.name,'paused','r-build',metadata={'reason':'no_progress'},now=100)
        health={'sessions':{'r-build':{'until':3700,'reason':'no_progress'}}}
        value=read_blockers(self.tmp.name,self.rows,health,{},now=200)['r-build']
        self.assertEqual(value['evidence']['reason'],'no_progress')
        self.assertEqual(value['evidence']['observed_at'],'1970-01-01T00:01:40Z')
        self.assertIn('underlying cause is not recorded',value['why'])
        record(self.tmp.name,'dispatch','r-build',now=150)
        self.assertNotIn('evidence',read_blockers(self.tmp.name,self.rows,health,{},now=200)['r-build'])

    def test_unrelated_pause_reason_does_not_become_no_progress_evidence(self):
        record(self.tmp.name,'paused','r-build',metadata={'reason':'blocked_quota'},now=100)
        value=read_blockers(self.tmp.name,self.rows,{'sessions':{'r-build':{'until':3700,'reason':'no_progress'}}},{},now=200)['r-build']
        self.assertNotIn('evidence',value)


if __name__=='__main__':
    unittest.main()
