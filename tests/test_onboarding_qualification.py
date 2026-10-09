import unittest
from unittest.mock import patch
from tests import test_onboarding as setup_tests


class QualificationReuseTests(unittest.TestCase):
    setUp=setup_tests.SetupTests.setUp
    connected=setup_tests.SetupTests.connected
    service=setup_tests.SetupTests.service

    def test_missing_controller_bypass_blocks_setup_verification(self):
        self.connected(); service, _, _ = self.service()
        report = service.github_class.return_value.protection_report.return_value
        report['bypass'] = {'status': 'missing'}
        self.setup.queue('check', self.setup.view()['revision'])
        for _ in range(3): service.tick()
        self.assertFalse(self.setup.view()['can_start'])
        self.assertIn('protection needs attention', self.setup.view()['error'])

    def test_controller_bypass_removed_after_verification_blocks_start(self):
        self.connected(); service, _, _ = self.service()
        self.setup.queue('check', self.setup.view()['revision'])
        for _ in range(20): service.tick()
        self.assertTrue(self.setup.view()['can_start'])
        service.github_class.return_value.protection_report.return_value['bypass'] = {'status': 'missing'}
        self.setup.queue('start', self.setup.view()['revision'])
        service.tick()
        self.assertFalse((self.root / 'state/factory-v2.enabled').exists())
        self.assertIn('protection needs attention', self.setup.view()['error'])

    def test_fresh_repository_recheck_does_not_run_paid_probes(self):
        self.connected();service,engine,resets=self.service()
        self.setup.queue('check',self.setup.view()['revision'])
        for _ in range(20):service.tick()
        self.assertTrue(self.setup.view()['can_start'])
        with patch.object(service,'new_probe',side_effect=AssertionError('Paid probe repeated')):
            self.setup.queue('check',self.setup.view()['revision'])
            service.tick();service.tick()
        self.assertTrue(self.setup.view()['can_start'])

    def test_expired_qualification_does_not_authorize_start(self):
        self.connected();service,engine,resets=self.service()
        self.setup.queue('check',self.setup.view()['revision'])
        for _ in range(20):service.tick()
        with self.setup.store.transaction() as data:data['qualification']['at']-=901
        self.setup.queue('start',self.setup.view()['revision'])
        service.tick()
        self.assertFalse((self.root/'state/factory-v2.enabled').exists())
        self.assertIn('expired',self.setup.view()['error'])

    def test_explicit_account_requalification_invalidates_old_evidence(self):
        self.connected();service,engine,resets=self.service()
        self.setup.queue('check',self.setup.view()['revision'])
        for _ in range(20):service.tick()
        self.assertTrue(self.setup.view()['qualification_fresh'])
        self.setup.queue('harness_check',self.setup.view()['revision'])
        self.assertFalse(self.setup.view()['qualification_fresh'])
        self.assertFalse(self.setup.view()['can_start'])


    def test_image_verified_survives_capacity_edits_until_the_image_changes(self):
        self.connected();service,engine,resets=self.service()
        self.assertFalse(self.setup.view()['image_verified'])
        self.setup.queue('check',self.setup.view()['revision'])
        for _ in range(20):service.tick()
        self.assertTrue(self.setup.view()['image_verified'])
        with self.setup.store.transaction() as data:data['qualification']['at']-=901
        self.assertFalse(self.setup.view()['qualification_fresh'])
        self.assertTrue(self.setup.view()['image_verified'])
        with self.setup.store.transaction() as data:data['qualification']['image']='sha256:other'
        self.assertFalse(self.setup.view()['image_verified'])
