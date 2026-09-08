import copy
import json
from pathlib import Path
import tempfile
import unittest
from forge.config import Repository
from forge.scheduling import (SchedulingGitHub, triage, repo_state, fingerprint,
                              can_set_priority, priority_inputs, issue_revision, TRIAGE_POLICIES)


def issue(number, names=('ready',), body='work', title='Title'):
    return {'id': 1000 + number, 'number': number, 'title': title, 'body': body,
            'state': 'open', 'repository_url': 'https://api.github.com/repos/X/y',
            'labels': [{'name': name} for name in names]}


class FakeGitHub:
    def __init__(self, rows, dependencies=None):
        self.rows = {row['number']: copy.deepcopy(row) for row in rows}
        self.dependencies = copy.deepcopy(dependencies or {})
        self.writes = []
        self.before_get = None

    def owned(self, config):
        return {'auto:owned-by-forge', 'priority:auto', 'priority:manual', 'priority:retriage', 'p0', 'p1', 'p2', 'p3'}

    def issues(self, config):
        return copy.deepcopy(list(self.rows.values()))

    def items(self, config):
        # Rows may include pull requests (``pull_request`` key); ``issues`` mocks still apply.
        return self.issues(config)

    def dependency_map(self, repo, issues, extra=()):
        return copy.deepcopy(self.dependencies)

    def request(self, method, endpoint, data=None):
        number = int(endpoint.split('/issues/')[1].split('/')[0]) if '/issues/' in endpoint else None
        if method == 'GET':
            if self.before_get:
                self.before_get(self.rows[number])
            return copy.deepcopy(self.rows[number])
        self.writes.append((method, endpoint, copy.deepcopy(data)))
        if method == 'POST' and endpoint.endswith('/labels') and number:
            names = {label['name'] for label in self.rows[number]['labels']} | set(data['labels'])
            self.rows[number]['labels'] = [{'name': name} for name in sorted(names)]
        elif method == 'DELETE' and '/labels/' in endpoint:
            old = endpoint.rsplit('/', 1)[1]
            self.rows[number]['labels'] = [l for l in self.rows[number]['labels'] if l['name'] != old]
        elif method == 'POST' and endpoint.endswith('/dependencies/blocked_by'):
            target = next(row for row in self.rows.values() if row['id'] == data['issue_id'])
            self.dependencies.setdefault(number, []).append(copy.deepcopy(target))
        return None


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = self.temp.name
        self.config = Repository('X/y', 'auto:owned-by-forge')

    def seed(self, records, **extra):
        path = repo_state(self.state, 'X/y')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'issues': records, **extra}))

    def model(self, repo, inputs):
        return {'issues': [{'number': row['number'],
                            'priority': next((x['name'] for x in row['labels'] if x['name'] in {'p0','p1','p2','p3'}), 'p2'),
                            'reason': 'proposed', 'dependencies': []} for row in inputs]}

    def run_triage(self, github, model=None, **kwargs):
        return triage(self.config, self.state, github=github, model=model or self.model, **kwargs)

    def test_manual_priority_is_preserved(self):
        github = FakeGitHub([issue(1, ('ready', 'p1', 'security'))])
        self.run_triage(github)
        self.assertEqual(github.writes, [])
        ledger = json.loads(repo_state(self.state, 'X/y').read_text())
        self.assertTrue(ledger['issues']['1']['manual'])

    def test_removing_auto_marker_or_priority_latches_manual(self):
        for names in [('ready', 'p1'), ('ready', 'priority:auto'), ('ready',)]:
            with self.subTest(names=names):
                self.seed({'1': {'assigned_priority': 'p1'}})
                github = FakeGitHub([issue(1, names)])
                self.run_triage(github, force=True)
                self.assertEqual(github.writes, [])
                ledger = json.loads(repo_state(self.state, 'X/y').read_text())
                self.assertTrue(ledger['issues']['1']['manual'])

    def test_add_priority_never_replaces_other_labels(self):
        github = FakeGitHub([issue(1, ('ready', 'security', 'backend'))])
        self.run_triage(github)
        names = {l['name'] for l in github.rows[1]['labels']}
        self.assertEqual(names, {'ready', 'security', 'backend', 'p2', 'priority:auto'})
        self.assertTrue(all(method == 'POST' for method, _, _ in github.writes))

    def test_title_body_label_or_claim_change_prevents_write(self):
        mutations = [lambda row: row.update(title='New emergency'),
                     lambda row: row.update(body='New instructions'),
                     lambda row: row['labels'].append({'name': 'wip'}),
                     lambda row: row['labels'].append({'name': 'hold'}),
                     lambda row: row['labels'].append({'name': 'p0'})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.seed({})
                github = FakeGitHub([issue(1)])
                github.before_get = mutate
                result = self.run_triage(github)
                self.assertEqual(github.writes, [])
                self.assertEqual(result['changes'][0]['status'], 'changed_during_triage')

    def test_held_and_wip_issues_are_untouched(self):
        github = FakeGitHub([issue(1, ('ready','hold')), issue(2, ('ready','wip'))])
        result = self.run_triage(github, lambda *a: self.fail('model should not run'))
        self.assertEqual(result['status'], 'no_pending_ready_work')
        self.assertEqual(github.writes, [])

    def test_existing_and_proposed_dependency_cycle_blocks_all_writes(self):
        one, two = issue(1, body='depends on #2'), issue(2)
        github = FakeGitHub([one, two], {2: [one]})
        def model(repo, inputs):
            value = self.model(repo, inputs)
            value['issues'][0]['dependencies'] = [{'number': 2, 'evidence': 'depends on #2'}]
            return value
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.run_triage(github, model)
        self.assertEqual(github.writes, [])

    def test_removed_automatic_dependency_is_not_reintroduced(self):
        github = FakeGitHub([issue(1, ('ready','p1'), 'depends on #2'), issue(2, ('ready','p1'))])
        self.seed({'1': {'dependencies': [1002]}})
        def model(repo, inputs):
            value = self.model(repo, inputs)
            value['issues'][0]['dependencies'] = [{'number': 2, 'evidence': 'depends on #2'}]
            return value
        self.run_triage(github, model)
        self.assertEqual(github.writes, [])
        ledger = json.loads(repo_state(self.state, 'X/y').read_text())
        self.assertEqual(ledger['issues']['1']['removed_dependencies'], [1002])

    def test_failure_persists_retry_cooldown(self):
        github = FakeGitHub([issue(1)])
        with self.assertRaisesRegex(RuntimeError, 'quota'):
            self.run_triage(github, lambda *a: (_ for _ in ()).throw(RuntimeError('quota')))
        result = self.run_triage(github, lambda *a: self.fail('retry ignored cooldown'))
        self.assertEqual(result['status'], 'cached_or_cooldown')
        self.assertEqual(github.writes, [])

    def test_unchanged_snapshot_uses_cache(self):
        rows = [issue(1)]
        self.seed({"1": {"triaged_revision": issue_revision(rows[0]), "triaged_policy_id": TRIAGE_POLICIES["llm"]}}, fingerprint=fingerprint(rows), policy_id=TRIAGE_POLICIES["llm"])
        result = self.run_triage(FakeGitHub(rows), lambda *a: self.fail('cache miss'))
        self.assertEqual(result['status'], 'cached_or_cooldown')

    def test_dependency_snapshot_includes_nonready_cycle_intermediates(self):
        github = SchedulingGitHub()
        calls = []
        rows = [issue(1), issue(2, ('hold',)), issue(3, ())]
        edges = {1: [rows[1]], 2: [rows[2]], 3: [rows[0]]}
        def pages(endpoint):
            calls.append(endpoint)
            number = int(endpoint.split('/issues/')[1].split('/')[0])
            return edges[number]
        github.pages = pages
        result = github.dependency_map('X/y', rows)
        self.assertEqual(set(result), {1, 2, 3})


    def test_referenced_wip_context_supplied_but_never_mutated(self):
        github = FakeGitHub([issue(1, ('ready','p1'), 'depends on #2'), issue(2, ('ready','wip','p1'))])
        def model(repo, inputs):
            self.assertEqual({r['number'] for r in inputs}, {1,2})
            value = self.model(repo, inputs)
            value['issues'][0]['dependencies'] = [{'number':2, 'evidence':'depends on #2'}]
            return value
        self.run_triage(github, model)
        self.assertEqual(len(github.writes), 1)
        self.assertTrue(github.writes[0][1].endswith('/issues/1/dependencies/blocked_by'))
        self.assertEqual(github.writes[0][2], {'issue_id':1002})

    def test_operator_can_release_priority_and_pending_revision_blocks_dispatch(self):
        from forge.scheduling import create_plan
        rows = [issue(1, ('ready', 'p3', 'priority:retriage'))]
        gh = FakeGitHub(rows)
        with tempfile.TemporaryDirectory() as state:
            config = Repository('X/y', 'auto:owned-by-forge')
            plan = create_plan(config.repo, rows, {}, state)
            self.assertEqual(plan['eligible'], [])
            self.assertIn('triage_pending', plan['blocked']['1'])
            proposal = {'issues': [{'number': 1, 'priority': 'p1', 'reason': 'explicit retriage', 'dependencies': []}]}
            triage(config, state, github=gh, model=lambda *args: proposal)
            updated = list(gh.rows.values())
            # Fake transport has no URL decoding for deletion; represent final remote labels.
            updated[0]['labels'] = [{'name': n} for n in ('ready', 'p1', 'priority:auto')]
            self.assertEqual(create_plan(config.repo, updated, {}, state)['eligible'], [1])
            updated[0]['title'] = 'Changed scope'
            self.assertEqual(create_plan(config.repo, updated, {}, state)['eligible'], [])


    def test_matching_fingerprint_without_triaged_revision_requires_triage(self):
        rows = [issue(1)]
        self.seed({}, fingerprint=fingerprint(rows))
        calls = []
        def model(repo, inputs):
            calls.append(inputs)
            return self.model(repo, inputs)
        self.run_triage(FakeGitHub(rows), model)
        self.assertEqual(len(calls), 1)


    def test_policy_mode_switch_retriages_but_same_policy_caches(self):
        github = FakeGitHub([issue(1, ('ready','p1'))])
        calls = []
        def model(repo, inputs):
            calls.append(inputs)
            return self.model(repo, inputs)
        self.run_triage(github, model, policy_id=TRIAGE_POLICIES['local'])
        self.run_triage(github, model, policy_id=TRIAGE_POLICIES['local'])
        self.assertEqual(len(calls), 1)
        self.run_triage(github, model, policy_id=TRIAGE_POLICIES['llm'])
        self.run_triage(github, model, policy_id=TRIAGE_POLICIES['llm'])
        self.assertEqual(len(calls), 2)
        ledger = json.loads(repo_state(self.state, 'X/y').read_text())
        self.assertEqual(ledger['policy_id'], TRIAGE_POLICIES['llm'])
        self.assertEqual(ledger['issues']['1']['triaged_policy_id'], TRIAGE_POLICIES['llm'])

    def test_mode_switch_preserves_failure_cooldown_unless_forced(self):
        github = FakeGitHub([issue(1, ('ready','p1'))])
        def fail(*args): raise RuntimeError('provider unavailable')
        with self.assertRaises(RuntimeError):
            self.run_triage(github, fail, policy_id=TRIAGE_POLICIES['local'])
        result = self.run_triage(github, lambda *a: self.fail('cooldown bypassed'), policy_id=TRIAGE_POLICIES['llm'])
        self.assertEqual(result['status'], 'cached_or_cooldown')
        result = self.run_triage(github, policy_id=TRIAGE_POLICIES['llm'], force=True)
        self.assertEqual(result['status'], 'applied')

    def test_next_cli_failure_has_empty_stdout_and_nonzero_exit(self):
        import contextlib
        import io
        from unittest.mock import patch
        from forge.scheduling import main
        for configurations, repo_args in [([self.config], ['--repo','X/y']), ([self.config], [])]:
            stdout, stderr = io.StringIO(), io.StringIO()
            github = FakeGitHub([])
            github.issues = lambda config: (_ for _ in ()).throw(RuntimeError('API unavailable'))
            with patch('forge.scheduling.load_repositories', return_value=configurations), patch('forge.scheduling.SchedulingGitHub', return_value=github), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(['next', '--mode','local', *repo_args])
            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('API unavailable', stderr.getvalue())

    def test_next_cli_admits_the_queue_behind_a_handed_off_claim(self):
        import contextlib
        import io
        from unittest.mock import patch
        from forge.scheduling import main
        pr = {**issue(30, ('ready-for-audit', 'critical'), body='Closes #10'), 'pull_request': {}, 'user': {'login': 'forge-bot'}}
        rows = [issue(10, ('wip',)), issue(11, ('ready',)), pr]
        self.seed({'11': {'triaged_revision': issue_revision(rows[1])}})
        for extra_pr, expected in ((None, '11\n'), ({**issue(31, ('changes-requested',), body='Fixes #10'), 'pull_request': {}, 'user': {'login': 'forge-bot'}}, '')):
            github = FakeGitHub(rows + ([extra_pr] if extra_pr else []))
            stdout = io.StringIO()
            with patch('forge.scheduling.load_repositories', return_value=[self.config]), \
                 patch('forge.scheduling.SchedulingGitHub', return_value=github), contextlib.redirect_stdout(stdout):
                code = main(['next', '--mode', 'local', '--repo', 'X/y', '--state', self.state])
            self.assertEqual(code, 0)
            # Audited claim handed off: the next issue is admitted. Sent back too: builder is reserved again.
            self.assertEqual(stdout.getvalue(), expected)

    def test_next_cli_does_not_emit_earlier_candidate_if_later_repo_fails(self):
        import contextlib
        import io
        from unittest.mock import patch
        from forge.scheduling import main
        github = FakeGitHub([])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('forge.scheduling.load_repositories', return_value=[self.config, Repository('X/z','auto:owned-by-forge')]), patch('forge.scheduling.SchedulingGitHub', return_value=github), patch('forge.scheduling.create_plan', side_effect=[{'eligible':[1]}, RuntimeError('later failure')]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['next', '--mode','local'])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('later failure', stderr.getvalue())

    def test_cli_passes_explicit_versioned_policy_identifier(self):
        import contextlib
        import io
        from unittest.mock import patch
        from forge.scheduling import main
        for mode in ('local','llm'):
            with patch('forge.scheduling.load_repositories', return_value=[self.config]), patch('forge.scheduling.triage', return_value={'status':'applied'}) as mocked, patch.dict('os.environ', {'FORGE_LLM_TRIAGE_ALLOWED':'1'}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['triage','--mode',mode]), 0)
            self.assertEqual(mocked.call_args.kwargs['policy_id'], TRIAGE_POLICIES[mode])

    def test_model_omission_cannot_admit_dependent_before_prerequisite(self):
        from forge.scheduling import create_plan
        github = FakeGitHub([issue(1, ('ready','p0'), 'depends on #2'), issue(2, ('ready','p3'))])
        self.run_triage(github)  # Deliberately returns dependencies: [] for every issue.
        plan = create_plan('X/y', list(github.rows.values()), github.dependencies, self.state)
        self.assertEqual(plan['eligible'], [2])
        self.assertEqual(plan['priorities']['2'], 'p0')
        self.assertEqual(github.dependencies[1][0]['number'], 2)

    def test_closed_url_prerequisite_is_fetched_and_cancellation_blocks(self):
        from forge.scheduling import create_plan
        for state_reason, expected in [('not_planned', []), ('completed', [1]), (None, [])]:
            with self.subTest(state_reason=state_reason), tempfile.TemporaryDirectory() as state:
                one = issue(1, ('ready','p0'), 'Requires https://github.com/X/y/issues/2')
                two = dict(issue(2, ('p3',)), state='closed', state_reason=state_reason)
                github = FakeGitHub([one, two])
                github.issues = lambda config: [copy.deepcopy(github.rows[1])]
                triage(self.config, state, github=github, model=self.model)
                plan = create_plan('X/y', [github.rows[1]], github.dependencies, state)
                self.assertEqual(plan['eligible'], expected)
                self.assertEqual(github.dependencies[1][0]['id'], 1002)
                self.assertFalse(any('/issues/2/' in endpoint for _, endpoint, _ in github.writes))

    def test_foreign_reference_not_fetched_or_converted(self):
        github = FakeGitHub([issue(1, ('ready','p1'), 'Depends on https://github.com/other/repo/issues/999')])
        self.run_triage(github)
        self.assertEqual(github.writes, [])

    def test_missing_referenced_context_blocks_before_model_or_remote_write(self):
        github = FakeGitHub([issue(1, ('ready','p1'), 'Depends on #999')])
        with self.assertRaises(KeyError):
            self.run_triage(github, lambda *a: self.fail('unknown prerequisite reached model'))
        self.assertEqual(github.writes, [])

    def test_deterministic_completion_preserves_manual_dependency_removal(self):
        github = FakeGitHub([issue(1, ('ready','p1'), 'depends on #2'), issue(2, ('ready','p1'))])
        self.seed({'1': {'dependencies': [1002]}})
        self.run_triage(github)  # Neither the model nor deterministic completion may restore it.
        self.assertEqual(github.writes, [])
        ledger = json.loads(repo_state(self.state, 'X/y').read_text())
        self.assertEqual(ledger['issues']['1']['removed_dependencies'], [1002])

    def test_reference_closure_has_hard_budget(self):
        rows = [issue(1, ('ready','p1'), '\n'.join('Depends on #' + str(n) for n in range(2, 152)))]
        rows += [issue(n, ()) for n in range(2, 152)]
        github = FakeGitHub(rows)
        with self.assertRaisesRegex(Exception, 'admission budget'):
            self.run_triage(github, lambda *a: self.fail('oversized context reached model'))
        self.assertEqual(github.writes, [])

    def test_ordinary_issue_or_pr_references_do_not_fetch_or_block(self):
        for body in ('Fixed by #99', 'Coordinate with #99',
                     'Complements https://github.com/X/y/issues/99',
                     'Does not depend on #99'):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as state:
                github = FakeGitHub([issue(1, ('ready','p1'), body)])
                result = triage(self.config, state, github=github, model=self.model)
                self.assertEqual(result['status'], 'applied')
                self.assertEqual(github.writes, [])

    def test_explicit_pr_prerequisite_rejects_before_model_or_writes(self):
        one = issue(1, ('ready','p1'), 'Depends on #99')
        pr = dict(issue(99, ()), pull_request={'url': 'https://api.github.com/repos/X/y/pulls/99'})
        github = FakeGitHub([one, pr])
        github.issues = lambda config: [copy.deepcopy(github.rows[1])]
        with self.assertRaisesRegex(Exception, 'context is missing or outside repository'):
            self.run_triage(github, lambda *a: self.fail('PR prerequisite reached model'))
        self.assertEqual(github.writes, [])

    def test_prerequisite_context_does_not_recurse_ordinary_related_graph(self):
        one = issue(1, ('ready','p1'), 'Depends on #2')
        two = issue(2, (), 'Fixed by #99 and coordinate with #98')
        github = FakeGitHub([one, two])
        self.run_triage(github)
        self.assertEqual(github.dependencies[1][0]['number'], 2)
