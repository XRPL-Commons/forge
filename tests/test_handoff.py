import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from forge.control import atomic_json, snapshot_path
from forge.handoff import check, idle_prompt, release


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'repos.d'
        self.config.mkdir()
        self.clone = self.root / 'clone'
        (self.config / 'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-forge\nCLONE_BUILD={self.clone}\n')
        self.state = self.root / 'state'
        self.snapshot = dict(repo='o/one', enabled=True, status='ok', observed_at=99,
                             counts={'builder': 0}, schedule={'active': [1]}, work={'review': [2], 'audit': []})
        self.save()
        self.head = 'a' * 40
        self.main_head = self.head
        self.branch = 'main'
        self.ancestor = False
        self.pr = dict(state='OPEN', isDraft=False, headRefOid=self.head, baseRefOid='c' * 40,
                       labels=[{'name': 'ready-for-review'}], author={'login': 'forge-bot'},
                       closingIssuesReferences=[{'number': 1, 'repository': {'nameWithOwner': 'o/one'}}])
        self.pane = 'Done.\n❯\u00a0\n────\n ⏵⏵ bypass permissions on (shift+tab to cycle)'
        self.processes = '10 1 bash\n11 10 claude\n20 1 unrelated'
        self.identity = 'one-build|$1|50|%1|10|claude'
        self.status = ''
        self.remote = 'git@github.com:o/one.git'
        self.calls = []
        self.pr_reads = 0
        self.change_on_second = None
        self.live_queue = []
        self.published_main = 'f' * 40
        self.main_reads = 0
        self.compare_status = 'diverged'
        self.compare_base = None
        self.on_second_main = None
        self.tracking_remote = ''
        self.tracking_branch = ''
        self.merged_prs = []
        self.trees = {}

    def test_native_provider_preserves_exact_quiet_process_proof(self):
        from forge.handoff import session_identity, Unverified
        self.identity=self.identity.replace('|claude','|claude.exe')
        self.processes=self.processes.replace('10 claude','10 claude.exe')
        self.assertEqual(session_identity('one-build',self.fake_run)['processes'],[10,11])
        self.processes+='\n12 11 node'
        with self.assertRaises(Unverified):session_identity('one-build',self.fake_run)
        self.processes='10 1 bash\n11 10 claude'
        with self.assertRaises(Unverified):session_identity('one-build',self.fake_run)
        self.identity=self.identity.replace('|claude.exe','|other-claude.exe')
        with self.assertRaises(Unverified):session_identity('one-build',self.fake_run)

    def save(self):
        atomic_json(snapshot_path(self.state, 'o/one'), self.snapshot)

    def fake_run(self, args, **kwargs):
        self.calls.append(args)
        text = ''
        if args[0] == 'git':
            cmd = args[3:]
            if cmd == ['rev-parse', '--show-toplevel']:
                text = str(self.clone)
            elif cmd == ['remote', 'get-url', 'origin']:
                text = self.remote
            elif cmd[0] == 'status':
                text = self.status
            elif cmd == ['rev-parse', 'HEAD']:
                text = self.head
            elif cmd == ['rev-parse', 'origin/main']:
                text = self.main_head
            elif cmd in (['symbolic-ref', '--short', 'HEAD'], ['symbolic-ref', '--quiet', '--short', 'HEAD']):
                text = self.branch
            elif cmd[:2] == ['config', '--get']:
                text = self.tracking_remote if cmd[-1].endswith('.remote') else self.tracking_branch
            elif cmd[0] == 'rev-parse' and cmd[-1].endswith('^{tree}'):
                text = self.trees.get(cmd[-1], '')
                if not text:
                    return SimpleNamespace(returncode=1, stdout='', stderr='')
            elif cmd[:2] == ['merge-base', '--is-ancestor']:
                return SimpleNamespace(returncode=0 if self.ancestor else 1, stdout='', stderr='')
            else:
                self.fail(args)
        elif args[:3] == ['gh', 'pr', 'list']:
            text = json.dumps(self.merged_prs if 'merged' in args else self.live_queue)
        elif args[:2] == ['gh', 'api']:
            if '/git/ref/' in args[2]:
                self.main_reads += 1
                if self.main_reads == 2 and self.on_second_main:
                    self.on_second_main()
                text = json.dumps({'object': {'sha': self.published_main}})
            elif '/compare/' in args[2]:
                text = json.dumps({'status': self.compare_status, 'base_commit': {'sha': self.compare_base or self.head}, 'behind_by': 0})
            else:
                self.fail(args)
        elif args[0] == 'gh':
            self.pr_reads += 1
            if self.pr_reads == 2 and self.change_on_second:
                self.change_on_second()
            text = json.dumps(self.pr)
        elif args[:2] == ['tmux', 'list-panes']:
            text = self.identity
        elif args[:2] == ['tmux', 'capture-pane']:
            text = self.pane
        elif args[0] == 'ps':
            text = self.processes
        elif args[:2] != ['tmux', 'kill-session']:
            self.fail(args)
        return SimpleNamespace(returncode=0, stdout=text, stderr='')

    def decision(self):
        return check(self.config, self.state, 'one-build', run=self.fake_run, now=100)

    def test_verified_handoff_is_read_only(self):
        self.assertTrue(self.decision()['eligible'])
        self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_explicit_prompt_required(self):
        self.assertFalse(idle_prompt('Nothing happened'))
        self.assertFalse(idle_prompt('❯ approve this\n bypass permissions on'))
        self.assertFalse(idle_prompt('❯\n bypass permissions on\n❯ unsent input\n bypass permissions on'))
        self.assertFalse(idle_prompt('❯\n bypass permissions on · esc to interrupt'))
        self.assertTrue(idle_prompt('old esc to interrupt\n❯\n bypass permissions on'))

    def test_faint_suggestion_is_not_unsent_input(self):
        footer = '\n\x1b[38;5;244m────\n\x1b[39m bypass permissions on'
        suggestion = '\x1b[39m❯\xa0\x1b[2mContinue your loop\x1b[0m'
        self.assertTrue(idle_prompt(suggestion + footer))
        self.assertFalse(idle_prompt('❯ Continue your loop' + footer))
        self.assertFalse(idle_prompt('❯ typed \x1b[2msuggestion\x1b[0m' + footer))
        self.assertFalse(idle_prompt(suggestion + footer + ' · esc to interrupt'))
        self.assertFalse(idle_prompt('❯ \x1b[38;5;2mgreen typed input' + footer))
        self.assertFalse(idle_prompt('❯ \x1b[2msuggestion\x1b[22m typed' + footer))

    def test_suggested_continuation_still_checks_all_handoff_evidence(self):
        self.pane = '❯\xa0\x1b[2mRun forge next again\x1b[0m\n bypass permissions on'
        self.assertTrue(self.decision()['eligible'])
        self.assertTrue(any(args[:3] == ['tmux', 'capture-pane', '-e'] for args in self.calls))
        self.status = ' M unfinished.py'
        self.assertFalse(self.decision()['eligible'])

    def test_fresh_complete_snapshot_required(self):
        for update in ({'observed_at': -300}, {'observed_at': 101}, {'status': 'error'}, {'enabled': False}):
            with self.subTest(update=update):
                old = self.snapshot.copy()
                self.snapshot.update(update)
                self.save()
                self.assertFalse(self.decision()['eligible'])
                self.snapshot = old

    def test_claim_and_queue_required(self):
        self.snapshot['schedule']['active'] = [1, 3]
        self.save()
        self.assertFalse(self.decision()['eligible'])
        self.snapshot['schedule']['active'] = [1]
        self.snapshot['counts']['builder'] = 1
        self.save()
        self.assertFalse(self.decision()['eligible'])

    def test_unpublished_or_unowned_work_never_released(self):
        for key, value in [('headRefOid', 'b' * 40), ('state', 'CLOSED'), ('isDraft', True),
                           ('author', {'login': 'other'}), ('closingIssuesReferences', []),
                           ('labels', [{'name': 'ready-for-review'}, {'name': 'hold'}])]:
            with self.subTest(key=key):
                prior = self.pr[key]
                self.pr[key] = value
                self.assertFalse(self.decision()['eligible'])
                self.pr[key] = prior

    def test_dirty_clone_wrong_remote_or_background_work_rejected(self):
        for attr, value in [('status', '?? unpublished.txt'), ('remote', 'git@github.com:o/other.git'),
                            ('processes', self.processes + '\n12 11 node'),
                            ('identity', self.identity + '\none-build|$1|50|%2|30|claude')]:
            with self.subTest(attr=attr):
                old = getattr(self, attr)
                setattr(self, attr, value)
                self.assertFalse(self.decision()['eligible'])
                setattr(self, attr, old)

    def test_release_rechecks_and_records_only_after_success(self):
        (self.state / 'one-build.up').touch()
        result = release(self.config, self.state, 'one-build', run=self.fake_run, now=100)
        self.assertTrue(result['released'])
        self.assertEqual(self.pr_reads, 2)
        self.assertEqual(self.calls[-1], ['tmux', 'kill-session', '-t', '$1'])
        self.assertFalse((self.state / 'one-build.up').exists())
        self.assertTrue((self.state / 'handoffs' / 'one-build.json').exists())

    def test_changed_generation_prevents_kill(self):
        self.change_on_second = lambda: setattr(self, 'identity', 'one-build|$2|90|%2|10|claude')
        result = release(self.config, self.state, 'one-build', run=self.fake_run, now=100)
        self.assertFalse(result['released'])
        self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_stage_change_before_release_prevents_kill(self):
        self.change_on_second = lambda: self.pr.update(labels=[{'name': 'changes-requested'}])
        self.assertFalse(release(self.config, self.state, 'one-build', run=self.fake_run, now=100)['released'])
        self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_dirty_after_pr_read_prevents_release(self):
        self.change_on_second = lambda: setattr(self, 'status', ' M changed.py')
        self.assertFalse(release(self.config, self.state, 'one-build', run=self.fake_run, now=100)['released'])
        self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_failed_kill_preserves_marker(self):
        (self.state / 'one-build.up').touch()
        def failed_kill(args, **kwargs):
            if args[:2] == ['tmux', 'kill-session']:
                return SimpleNamespace(returncode=1, stdout='', stderr='failed')
            return self.fake_run(args, **kwargs)
        self.assertFalse(release(self.config, self.state, 'one-build', run=failed_kill, now=100)['released'])
        self.assertTrue((self.state / 'one-build.up').exists())

    def returned_main_setup(self):
        self.pr['baseRefOid'] = self.head
        self.pr['headRefOid'] = 'b' * 40

    def test_builder_returned_to_published_main_can_release(self):
        self.returned_main_setup()
        result = release(self.config, self.state, 'one-build', run=self.fake_run, now=100)
        self.assertTrue(result['released'])
        self.assertEqual(result['evidence']['checkout_basis'], 'published_main')
        self.assertEqual(result['evidence']['pr_head'], 'b' * 40)

    def test_returned_builder_requires_main_to_match_live_pr_base(self):
        self.returned_main_setup()
        self.main_head = 'd' * 40
        self.assertFalse(self.decision()['eligible'])
        self.main_head = self.head
        self.pr['baseRefOid'] = 'e' * 40
        self.assertFalse(self.decision()['eligible'])

    def test_returned_builder_pr_head_or_base_change_prevents_release(self):
        for field in ['headRefOid', 'baseRefOid']:
            with self.subTest(field=field):
                self.returned_main_setup()
                self.pr_reads = 0
                self.calls = []
                self.change_on_second = lambda: self.pr.update({field: 'e' * 40})
                self.assertFalse(release(self.config, self.state, 'one-build', run=self.fake_run, now=100)['released'])
                self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_returned_builder_main_ref_change_during_check_prevents_release(self):
        self.returned_main_setup()
        self.change_on_second = lambda: setattr(self, 'main_head', 'e' * 40)
        self.assertFalse(release(self.config, self.state, 'one-build', run=self.fake_run, now=100)['released'])

    def review_setup(self):
        (self.config / 'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-forge\nCLONE_REVIEW={self.clone}\n')
        self.snapshot['counts']['review'] = 0
        self.snapshot['work'] = {'review': [], 'audit': [2]}
        self.save()
        self.pr['baseRefOid'] = self.head
        self.pr['headRefOid'] = 'b' * 40
        self.pr['labels'] = [{'name': 'ready-for-audit'}, {'name': 'critical'}]
        self.identity = 'one-review|$1|50|%1|10|claude'

    def test_completed_reviewer_can_release_without_changing_approval_gate(self):
        self.review_setup()
        result = release(self.config, self.state, 'one-review', run=self.fake_run, now=100)
        self.assertTrue(result['released'])
        self.assertEqual(result['evidence']['role'], 'review')
        self.assertEqual(self.pr['labels'], [{'name': 'ready-for-audit'}, {'name': 'critical'}])
        self.assertTrue(all(args[1:3] == ['pr', 'view'] for args in self.calls if args[0] == 'gh'))

    def test_reviewer_on_exact_published_pr_head_can_release(self):
        self.review_setup()
        self.head = self.pr['headRefOid']
        result = release(self.config, self.state, 'one-review', run=self.fake_run, now=100)
        self.assertTrue(result['released'])
        self.assertEqual(result['evidence']['checkout_basis'], 'pr_head')

    def test_reviewer_on_unpublished_commit_cannot_release(self):
        self.review_setup()
        self.head = 'd' * 40
        self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])

    def test_reviewer_pr_head_change_between_checks_prevents_release(self):
        self.review_setup()
        self.head = self.pr['headRefOid']
        self.change_on_second = lambda: self.pr.update(headRefOid='e' * 40)
        self.assertFalse(release(self.config, self.state, 'one-review', run=self.fake_run, now=100)['released'])
        self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def stale_main_setup(self):
        self.head = 'a' * 40
        self.main_head = self.head
        self.review_setup()
        self.head = 'd' * 40
        self.ancestor = True

    def test_reviewer_clean_stale_main_is_safe_published_ancestor(self):
        self.stale_main_setup()
        result = release(self.config, self.state, 'one-review', run=self.fake_run, now=100)
        self.assertTrue(result['released'])
        self.assertEqual(result['evidence']['checkout_basis'], 'published_main_ancestor')
        self.assertEqual(result['evidence']['branch'], 'main')

    def test_stale_main_nonancestor_or_other_branch_rejected(self):
        self.stale_main_setup()
        self.ancestor = False  # Includes ahead or diverged commits.
        self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])
        self.ancestor = True
        self.branch = 'unfinished-review'
        self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])

    def test_stale_main_remote_ref_or_branch_change_prevents_release(self):
        for attr, value in [('main_head', 'e' * 40), ('branch', 'unfinished')]:
            with self.subTest(attr=attr):
                self.stale_main_setup()
                self.main_head = self.pr['baseRefOid']
                self.branch = 'main'
                self.pr_reads = 0
                self.calls = []
                self.change_on_second = lambda: setattr(self, attr, value)
                self.assertFalse(release(self.config, self.state, 'one-review', run=self.fake_run, now=100)['released'])
                self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_reviewer_with_pending_review_cannot_release(self):
        self.review_setup()
        self.snapshot['counts']['review'] = 1
        self.save()
        self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])

    def test_reviewer_requires_live_audit_handoff_and_published_base(self):
        self.review_setup()
        self.pr['labels'] = [{'name': 'ready-for-review'}]
        self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])
        self.pr['labels'] = [{'name': 'ready-for-audit'}]
        self.pr['baseRefOid'] = 'c' * 40
        self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])

    def completed_review_setup(self):
        self.review_setup()
        self.snapshot['schedule']['active'] = []
        self.snapshot['work']['audit'] = []
        self.save()
        self.compare_status = 'ahead'

    def test_completed_review_after_merge_releases_from_published_main(self):
        self.completed_review_setup()
        result = release(self.config, self.state, 'one-review', run=self.fake_run, now=100)
        self.assertTrue(result['released'])
        self.assertEqual(result['reason'], 'verified_review_complete')
        self.assertEqual(self.main_reads, 2)

    def test_completed_review_live_queue_dirt_unpublished_or_wrong_branch_reject(self):
        self.completed_review_setup()
        for attr, value in [('live_queue', [{'number': 3}]), ('status', '?? draft'),
                            ('compare_status', 'diverged'), ('compare_status', 'behind'),
                            ('compare_base', 'e' * 40), ('branch', 'unfinished')]:
            with self.subTest(attr=attr, value=value):
                old = getattr(self, attr)
                setattr(self, attr, value)
                self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])
                setattr(self, attr, old)

    def test_completed_review_main_advances_between_checks_no_release(self):
        self.completed_review_setup()
        self.on_second_main = lambda: setattr(self, 'published_main', 'e' * 40)
        self.assertFalse(release(self.config, self.state, 'one-review', run=self.fake_run, now=100)['released'])
        self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_completed_review_fallback_after_pr_merged_with_stale_snapshot(self):
        self.review_setup()
        self.pr['state'] = 'MERGED'
        self.compare_status = 'ahead'
        self.assertTrue(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])

    def test_generic_completion_never_applies_to_builders(self):
        self.snapshot['schedule']['active'] = []
        self.save()
        self.compare_status = 'ahead'
        self.assertFalse(self.decision()['eligible'])
        self.assertEqual(self.main_reads, 0)

    def completed_audit_setup(self):
        self.completed_review_setup()
        (self.config / 'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-forge\nCLONE_AUDIT={self.clone}\n')
        self.snapshot['counts']['audit'] = 0
        self.save()
        self.identity = 'one-audit|$1|50|%1|10|claude'

    def test_completed_auditor_reclaims_after_merge(self):
        self.completed_audit_setup()
        result = release(self.config, self.state, 'one-audit', run=self.fake_run, now=100)
        self.assertTrue(result['released'])
        self.assertEqual(result['reason'], 'verified_audit_complete')
        queries = [args for args in self.calls if args[:3] == ['gh', 'pr', 'list']]
        self.assertTrue(all('ready-for-audit' in args for args in queries))

    def test_auditor_waiting_approval_is_not_generic_empty_queue(self):
        self.completed_audit_setup()
        self.snapshot['counts']['audit'] = 1
        self.snapshot['work']['audit'] = [2]
        self.save()
        self.assertFalse(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])

    def test_auditor_new_live_candidate_blocks_release(self):
        self.completed_audit_setup()
        self.live_queue = [{'number': 3}]
        self.assertFalse(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])

    def historical_checkout_setup(self, role='review'):
        if role == 'audit':
            self.completed_audit_setup()
        else:
            self.completed_review_setup()
        self.branch = 'old-reviewed-branch'
        self.tracking_remote = 'origin'
        self.tracking_branch = 'refs/heads/old-reviewed-branch'
        self.historical_merge = 'd' * 40
        self.merged_prs = [{'number': 34, 'headRefOid': self.head,
                           'headRefName': 'old-reviewed-branch', 'state': 'MERGED',
                           'mergedAt': '2026-01-01', 'mergeCommit': {'oid': self.historical_merge}}]
        self.trees = {self.head+'^{tree}': 'e'*40, self.historical_merge+'^{tree}': 'e'*40}
        self.compare_base = self.historical_merge

    def test_completed_reviewer_on_exact_historical_merged_tree_can_release(self):
        self.historical_checkout_setup()
        result = release(self.config, self.state, 'one-review', run=self.fake_run, now=100)
        self.assertTrue(result['released'], result)
        proof = result['evidence']
        self.assertEqual(proof['checkout_basis'], 'merged_pr_tree_on_published_main')
        self.assertEqual(proof['checkout']['pr'], 34)
        self.assertEqual(proof['checkout']['merge'], self.historical_merge)
        self.assertEqual(proof['checkout']['verified_bases'], [self.published_main])
        self.assertTrue(any(self.historical_merge+'...'+self.published_main in ' '.join(c) for c in self.calls))
        self.assertFalse(any('fetch' in c or 'checkout' in c for c in self.calls))

    def test_completed_auditor_on_exact_historical_merged_tree_can_release(self):
        self.historical_checkout_setup(role='audit')
        self.assertTrue(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])

    def test_historical_branch_unpublished_tree_wrong_remote_or_live_work_rejects(self):
        self.historical_checkout_setup()
        for attr, value in [('tracking_remote', 'fork'), ('merged_prs', []),
                            ('trees', {self.head+'^{tree}': 'e'*40, self.historical_merge+'^{tree}': 'f'*40}),
                            ('compare_status', 'diverged'), ('compare_base', 'c'*40),
                            ('live_queue', [{'number': 99}]), ('status', ' M unfinished.py'),
                            ('pane', '❯ unsent input\n bypass permissions on')]:
            with self.subTest(attr=attr):
                old = getattr(self, attr)
                setattr(self, attr, value)
                self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])
                setattr(self, attr, old)

    def test_historical_checkout_change_between_verifications_prevents_release(self):
        self.historical_checkout_setup()
        self.on_second_main = lambda: setattr(self, 'tracking_branch', 'refs/heads/replaced')
        result = release(self.config, self.state, 'one-review', run=self.fake_run, now=100)
        self.assertFalse(result['released'])
        self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_unknown_read_fails_closed(self):
        def failure(*args, **kwargs):
            raise OSError('unavailable')
        self.assertFalse(check(self.config, self.state, 'one-build', run=failure, now=100)['eligible'])


if __name__ == '__main__':
    unittest.main()
