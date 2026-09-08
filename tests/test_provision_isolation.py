import argparse
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('provision_isolation', Path(__file__).resolve().parents[1] / 'deploy/provision_isolation.py')
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.payload = Path(self.temp.name) / 'client'
        self.payload.write_bytes(b'\x7fELFsynthetic-not-executable')
        self.args = argparse.Namespace(apply=False, codex_source=str(self.payload), claude_source=str(self.payload),
                                       codex_sha256=None, claude_sha256=None)

    def test_dry_run_never_executes_sources_or_mutates_accounts(self):
        with mock.patch.object(provision, 'run') as run, mock.patch.object(provision, 'check_identity'):
            plan = provision.make_plan(self.args)
        run.assert_not_called()
        self.assertEqual(plan['mode'], 'dry-run')
        self.assertEqual(plan['auth_credentials'], 'not copied')
        self.assertEqual(len(plan['clients']), 2)

    def test_apply_requires_reviewed_hashes(self):
        self.args.apply = True
        with self.assertRaisesRegex(ValueError, 'reviewed'):
            provision.make_plan(self.args)

    def test_changed_source_invalidates_pin(self):
        self.args.codex_sha256 = '0' * 64
        with self.assertRaisesRegex(ValueError, 'pin mismatch'):
            provision.make_plan(self.args)

    def test_source_symlinks_are_rejected(self):
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.payload)
        with self.assertRaises(OSError):
            provision.digest(alias)

    def test_profile_is_attached_to_only_pinned_executable(self):
        for provider in ('codex', 'claude'):
            profile = provision.profile(provider)
            self.assertIn(f'/opt/forge/bin/{provider} flags=(unconfined)', profile)
            self.assertIn('userns,', profile)
            self.assertNotIn('sysctl', profile)
            self.assertNotIn('/home/', profile)
