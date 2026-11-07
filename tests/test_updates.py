import unittest
from unittest.mock import Mock

from forge.app_access import AccessError
from forge.release_artifacts import ASSETS
from forge.updates import UpdateCheck

OLD, NEW = 'a' * 40, 'b' * 40


def release(revision=NEW, **changes):
    return dict(tag_name='forge-build-' + revision, published_at='2026-11-07T12:00:00Z',
                assets=[{'name': name} for name in [*ASSETS.values(), 'manifest.json']], **changes)


class UpdateTests(unittest.TestCase):
    def check(self, responses):
        request = Mock(side_effect=responses)
        checker = UpdateCheck(request=request, clock=lambda: 100, monotonic=lambda: 100)
        return checker, request

    def test_only_complete_published_builds_are_offered(self):
        incomplete = release(); incomplete['assets'] = []
        checker, request = self.check([[release(OLD), release(draft=True), release(prerelease=True), incomplete]])
        view = checker.view(OLD)
        self.assertEqual(view['status'], 'current')
        self.assertEqual(view['latest']['revision'], OLD)
        self.assertEqual(request.call_count, 1)
        self.assertIsNone(request.call_args.args[2], 'checks do not send personal or App credentials')

    def test_comparison_prevents_offering_a_downgrade(self):
        for github, status in [('ahead', 'available'), ('behind', 'ahead'), ('diverged', 'diverged')]:
            with self.subTest(github=github):
                checker, request = self.check([[release()], {'status': github}])
                self.assertEqual(checker.view(OLD)['status'], status)
                self.assertIn(OLD + '...' + NEW, request.call_args.args[1])
                self.assertIsNone(request.call_args.args[2])

    def test_unknown_local_build_does_not_claim_up_to_date(self):
        checker, request = self.check([[release()]])
        self.assertEqual(checker.view(None)['status'], 'unknown')
        self.assertEqual(request.call_count, 1)
        checker, _ = self.check([[release()], AccessError('missing commit', status=404)])
        self.assertEqual(checker.view(OLD)['status'], 'unknown')

    def test_private_repo_and_rate_limits_are_not_success(self):
        for status in [404, 403, 429]:
            checker, _ = self.check([AccessError('private upstream details', status=status)])
            view = checker.view(OLD)
            self.assertEqual(view['status'], 'unavailable')
            self.assertIsNone(view['latest'])
            self.assertNotIn('private upstream details', view['error'])

    def test_missing_releases_is_explicit(self):
        checker, _ = self.check([[]])
        self.assertEqual(checker.view(OLD)['status'], 'no_release')

    def test_checks_are_cached_and_manual_refresh_is_bounded(self):
        now = [100]
        request = Mock(return_value=[release(OLD)])
        checker = UpdateCheck(request=request, clock=lambda: now[0], monotonic=lambda: now[0])
        checker.view(OLD)
        now[0] += 30
        checker.view(OLD, refresh=True)
        self.assertEqual(request.call_count, 1)
        now[0] += 31
        checker.view(OLD)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(checker.view(OLD, refresh=True)['checked_at'], now[0])
        self.assertEqual(request.call_count, 2)
        now[0] += 3601
        checker.view(OLD)
        self.assertEqual(request.call_count, 3)

    def test_installed_version_change_invalidates_cache(self):
        checker, request = self.check([[release()], {'status': 'ahead'}, [release()]])
        self.assertEqual(checker.view(OLD)['status'], 'available')
        self.assertEqual(checker.view(NEW)['status'], 'current')
        self.assertEqual(request.call_count, 3)
