import json
import unittest

from forge.factory.github import GitHubUnavailable
from forge.factory.requirements_controller import RuntimeRequirementsController
from forge.store.effects import Effects
from forge.store.models import TransitionError
from tests import test_store_runtime_requirements as helpers


class EnvironmentEffectRetryTests(unittest.TestCase):
    setUp=helpers.RuntimeRequirementsTests.setUp
    snapshot=helpers.RuntimeRequirementsTests.snapshot
    command=helpers.RuntimeRequirementsTests.command

    def effect(self):
        with self.store.read_snapshot() as db:
            return dict(db.execute("SELECT * FROM external_effects WHERE kind='environment_label' ORDER BY created_at,id LIMIT 1").fetchone())

    def failing(self,item,remote,target):
        self.sends+=1
        raise GitHubUnavailable('network unavailable')

    def start_failure(self):
        self.sends=0
        self.github.environment_label=self.failing
        with self.assertRaises(GitHubUnavailable):self.controller.observe('u')
        return self.effect()

    def test_failed_send_backoff_survives_restart_and_does_not_create_new_intent(self):
        effect=self.start_failure()
        self.assertEqual((effect['attempts'],effect['retry_at']),(1,130))
        self.now=129
        restarted=RuntimeRequirementsController(self.store,self.github,config=self.config,clock=lambda:self.now)
        self.assertEqual(restarted.observe('u')['pending'],'environment_label')
        with self.assertRaises(TransitionError):self.commands.claim_environment(effect['id'],expected_version=self.snapshot()['unit']['version'])
        self.assertEqual(self.sends,1)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM external_effects').fetchone()[0],1)
        self.now=130
        with self.assertRaises(GitHubUnavailable):restarted.observe('u')
        self.assertEqual((self.effect()['attempts'],self.effect()['retry_at']),(2,190))
        self.assertEqual(self.sends,2)

    def test_uncertainty_bound_stops_sends_but_exact_readback_can_complete(self):
        self.controller.effects=Effects(self.store,clock=lambda:self.now,max_attempts=1)
        effect=self.start_failure()
        self.assertIsNotNone(self.effect()['escalation_at'])
        self.now=2000
        self.assertEqual(self.controller.observe('u')['pending'],'environment_label')
        self.assertEqual(self.sends,1)
        self.github.remote['labels']=['env:standard']
        self.controller.observe('u')
        self.assertEqual(self.effect()['status'],'confirmed')
        self.assertEqual(self.effect()['attempts'],1)

    def test_direct_claim_uses_canonical_config_cap_and_records_deadline_before_send(self):
        selection={'tier':'standard','automatic':True,'services':[]}
        result=self.commands.select_environment(self.command(),requirements_revision_id=self.revision,choice=selection,labels=[],config_hash='policy')
        with self.store.write() as db:
            db.execute("INSERT INTO configuration(domain,subject,revision,active,settings_json,created_at) VALUES('runtime.json','instance',1,1,?,100)",
                (json.dumps({'reliability':{'effect_max_attempts':1}}),))
        self.commands.claim_environment(result['effect_id'],expected_version=self.snapshot()['unit']['version'])
        self.assertEqual(self.effect()['retry_at'],130)
        with self.assertRaises(TransitionError):self.commands.claim_environment(result['effect_id'],expected_version=self.snapshot()['unit']['version'])
        self.now=130
        with self.assertRaises(TransitionError):self.commands.claim_environment(result['effect_id'],expected_version=self.snapshot()['unit']['version'])
        self.assertEqual(self.effect()['attempts'],1)

    def test_read_failure_does_not_add_physical_send_attempt(self):
        self.start_failure()
        self.now=130
        self.github.issue=lambda _:(_ for _ in ()).throw(GitHubUnavailable('read failed'))
        with self.assertRaises(GitHubUnavailable):self.controller.observe('u')
        self.assertEqual(self.effect()['attempts'],1)
        self.assertEqual(self.effect()['retry_at'],160)
