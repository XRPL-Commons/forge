import unittest
from copy import deepcopy
from forge.factory.operator_status import overview
from forge.factory.config import validate


class OperatorStatusTests(unittest.TestCase):
    def setUp(self):
        self.data={'issues':{},'jobs':{},'slots':{},'discovery_observed':{'1':1000}}
        self.repos=[{'id':1,'name':'example/project','enabled':True,'access_ready':True,'protection_ready':True,'setup_url':'/settings/repository?repo_id=1'}]
        self.runtime=deepcopy(validate({'enabled':True,'image':'sha256:'+'a'*64}))
        self.observation={'at':1000,'errors':[]}

    def view(self):return overview(self.data,self.repos,self.runtime,self.observation,True,1001)

    def test_no_work_requires_fresh_supervisor_and_repository_evidence(self):
        self.assertEqual(self.view()['title'],'Waiting for a ready issue')
        self.observation['at']=1
        self.assertEqual(self.view()['title'],'Waiting for the Forge service')
        self.assertNotIn('empty',self.view())

    def test_enabled_does_not_mean_repo_or_worker_ready(self):
        self.repos[0]['protection_ready']=False
        self.assertEqual(self.view()['title'],'Check repository setup')
        self.runtime['enabled']=False
        self.assertEqual(self.view()['title'],'Forge is paused')

    def test_ready_but_ineligible_is_not_an_empty_queue(self):
        self.data['issues']['1:1']={'number':1,'repo':'example/project','state':'ready','active':None}
        self.assertEqual(self.view()['title'],'Work is queued')
        self.assertNotIn('empty',self.view())

    def test_human_wait_has_actual_pr_and_reason(self):
        self.data['issues']['1:1']={'key':'1:1','number':1,'repo':'example/project','state':'approval_needed','active':None,'reason':'An authorized human must approve','candidate':{'pr':12}}
        self.assertEqual(self.view()['action_url'],'/issues?issue=1:1')
        self.assertIn('authorized human',self.view()['detail'])

    def test_active_role_is_evidence_not_completion(self):
        self.data['issues']['1:1']={'number':1,'repo':'example/project','state':'reviewed','active':'job','title':'Fix setup'}
        self.data['jobs']['job']={'role':'audit'}
        self.assertEqual(self.view()['title'],'Auditing issue #1')

    def test_provider_outage_is_actionable_and_no_success_inferred(self):
        self.data['issues']['1:1']={'number':1,'repo':'example/project','state':'ready','active':None}
        self.data['provider_retry_after']=1100
        self.assertEqual(self.view()['title'],'Waiting for a coding account')
        self.assertEqual(self.view()['action_url'],'/settings/harnesses')

    def test_current_fault_is_not_an_empty_queue(self):
        self.observation['errors']=[{'reason':'GitHub could not be reached'}]
        self.assertEqual(self.view()['title'],'Forge needs attention')
        self.assertNotIn('empty',self.view())

    def test_one_account_cooldown_does_not_hide_available_accounts(self):
        self.data['issues']['1:1']={'number':1,'repo':'example/project','state':'ready','active':None}
        self.runtime['provider_pool']=[{'id':'one','provider':'codex'}, {'id':'two','provider':'claude'}]
        self.data['provider_cooldowns']={'codex':{'until':1100}}
        self.assertEqual(self.view()['title'],'Work is queued')
        self.data['provider_cooldowns']['claude']={'until':1100}
        self.assertEqual(self.view()['title'],'Waiting for a coding account')

    def test_empty_queue_waits_for_actual_discovery(self):
        self.data['discovery_observed']={}
        self.assertEqual(self.view()['title'],'Looking for ready issues')
        self.runtime['slots']['build']=0
        self.assertEqual(self.view()['title'],'Builder capacity is paused')

    def test_active_failure_investigation_is_visible(self):
        self.data['issues']['1:1']={'number':1,'repo':'example/project','state':'failed','active':None}
        self.data['jobs']['recovery']={'role':'recovery','issue_key':'1:1','cleanup':False}
        self.assertEqual(self.view()['title'],'Investigating issue #1')

    def test_capacity_and_backoff_explain_queued_work(self):
        self.data['issues']['1:1']={'number':1,'repo':'example/project','state':'ready','active':None,'retry_after':1100,'reason':'Role credentials unavailable'}
        self.assertEqual(self.view()['title'],'Waiting before retrying')
        self.runtime['slots']['build']=0
        self.assertEqual(self.view()['title'],'Worker capacity is paused')

    def test_memory_wait_is_not_an_issue_failure(self):
        self.data['issues']['1:1']={'number':1,'repo':'example/project','state':'ready','active':None}
        self.observation['capacity']={'allowed':False,'reason':'memory_headroom'}
        self.assertEqual(self.view()['title'],'Waiting for available memory')
        self.assertIn('retry automatically',self.view()['detail'])
        self.observation['capacity']={'allowed':False,'reason':'telemetry_unavailable'}
        self.assertEqual(self.view()['title'],'Waiting for memory checks')
        self.observation['capacity']={'allowed':True}
        self.assertEqual(self.view()['title'],'Work is queued')
