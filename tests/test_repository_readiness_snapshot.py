"""Board readiness consumes its runtime snapshot without losing Settings fields."""
from unittest.mock import Mock, patch

from forge.factory.config import load, save
from forge.factory.store_views import PipelineViews
from tests import test_sqlite_runtime_support as fixture
from tests.test_sqlite_runtime_support import FULL


class RepositoryReadinessSnapshotTests(fixture.RuntimeFixture):
    def test_readiness_matches_settings_and_reuses_validated_runtime(self):
        self.prepare()
        runtime = load(self.root)
        runtime['environment_tiers'] = {'full': FULL}
        runtime['repository_merge_policies'] = {'10':'auto'}
        save(self.root, runtime)
        runtime = load(self.root)
        for at in (1000, 1400, 2000):
            self.now = at
            expected = self.settings.view()['repositories']
            with patch('forge.factory.config.load', side_effect=AssertionError('runtime reread')), \
                    patch('forge.repository_environments.validate', side_effect=AssertionError('runtime revalidation')), \
                    patch.object(self.settings, 'sync', side_effect=AssertionError('unused sync read')), \
                    patch.object(self.settings, 'configured', side_effect=AssertionError('unused settings header')):
                actual = self.settings.repository_views(runtime=runtime)
            self.assertEqual(actual, expected)
            self.assertEqual(actual[0]['merge_policy'], 'auto')
            self.assertTrue(actual[0]['environment']['full_available'])

    def test_board_and_issue_list_pass_exact_runtime_to_repository_reader(self):
        self.prepare()
        runtime = load(self.root)
        read_runtime = Mock(return_value=runtime)
        read_repositories = Mock(side_effect=lambda value:self.settings.repository_views(runtime=value))
        views = PipelineViews(self.database, runtime=read_runtime, repositories=read_repositories, clock=lambda:self.now)
        for read in (lambda:views.factory(section='board'), lambda:views.issues()):
            read_runtime.reset_mock(); read_repositories.reset_mock()
            result = read()
            read_runtime.assert_called_once_with()
            read_repositories.assert_called_once_with(runtime)
            self.assertIs(read_repositories.call_args.args[0], runtime)
            self.assertEqual(result['repositories'][0]['id'], 10)
