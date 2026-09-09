from tests.host_isolation import deny_host_tmux
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import fcntl
from unittest.mock import patch
from forge.human_wait import _locked as real_locked

from forge.control import atomic_json, snapshot_path
from forge.handoff import check, idle_prompt, release


class HandoffTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)
        self.lock_patch = patch('forge.human_wait._locked', return_value=True)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'repos.d'
        self.config.mkdir()
        self.clone = self.root / 'clone'
        (self.config / 'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_BUILD={self.clone}\n')
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
        self.remote_merge_tree = 'e' * 40
        self.remote_merge_sha = None
        self.detached_prs = []

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
                if self.branch is None:
                    return SimpleNamespace(returncode=1, stdout='', stderr='')
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
            elif '/commits/' in args[2] and '/pulls?' in args[2]:
                text = json.dumps(self.detached_prs)
            elif '/git/commits/' in args[2]:
                text = json.dumps({'sha': self.remote_merge_sha or args[2].rsplit('/', 1)[-1], 'tree': {'sha': self.remote_merge_tree}})
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
        (self.config / 'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_REVIEW={self.clone}\n')
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
        (self.config / 'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_AUDIT={self.clone}\n')
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

    def test_detached_completed_auditor_requires_published_ancestry(self):
        self.completed_audit_setup()
        self.branch = None
        self.published_main = self.head
        self.assertTrue(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])
        self.compare_status = 'diverged'
        self.assertFalse(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])

    def test_detached_checkout_change_between_checks_preserves_worker(self):
        self.completed_audit_setup()
        self.branch = None
        self.published_main = self.head
        self.on_second_main = lambda: setattr(self, 'branch', 'main')
        result = release(self.config, self.state, 'one-audit', run=self.fake_run, now=100)
        self.assertFalse(result['released'])
        self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_missing_merge_object_uses_exact_canonical_remote_tree(self):
        self.historical_checkout_setup()
        del self.trees[self.historical_merge + '^{tree}']
        result = check(self.config, self.state, 'one-review', run=self.fake_run, now=100)
        self.assertTrue(result['eligible'], result)
        self.assertTrue(any('/git/commits/' + self.historical_merge in ' '.join(c) for c in self.calls))
        self.remote_merge_tree = 'f' * 40
        self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])
        self.remote_merge_tree = 'e' * 40
        self.remote_merge_sha = 'f' * 40
        self.assertFalse(check(self.config, self.state, 'one-review', run=self.fake_run, now=100)['eligible'])

    def completed_builder_setup(self):
        self.historical_checkout_setup()
        self.identity = self.identity.replace('one-review', 'one-build')
        (self.config / 'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_BUILD={self.clone}\n')
        self.snapshot['counts']['builder'] = 0
        self.snapshot['schedule']['active'] = [1]
        self.snapshot['work'].update(ready=[], sentback=[], conflicts=[], limbo=[], review=[], audit=[])
        self.save()

    def test_completed_builder_requires_exact_merged_branch_and_no_actionable_work(self):
        self.completed_builder_setup()
        result = check(self.config, self.state, 'one-build', run=self.fake_run, now=100)
        self.assertTrue(result['eligible'], result)
        self.assertEqual(result['reason'], 'verified_build_complete')
        for key in ('sentback', 'conflicts', 'limbo'):
            self.snapshot['work'][key] = [9]
            self.save()
            self.assertFalse(self.decision()['eligible'])
            self.snapshot['work'][key] = []
        self.save()
        self.live_queue = [{'number': 9}]
        self.assertFalse(self.decision()['eligible'])
        self.live_queue = []
        self.branch = 'main'
        self.assertFalse(self.decision()['eligible'])

    def test_completed_builder_does_not_erase_untracked_files(self):
        self.completed_builder_setup()
        self.status = '?? .nuxtrc'
        self.assertFalse(self.decision()['eligible'])

    def test_detached_squashed_auditor_requires_canonical_pr_and_exact_tree(self):
        self.completed_audit_setup()
        self.branch = None
        merge = 'd' * 40
        self.detached_prs = [{'number': 130, 'state': 'closed', 'merged_at': '2026-09-08',
                             'head': {'sha': self.head}, 'base': {'repo': {'full_name': 'o/one'}},
                             'merge_commit_sha': merge}]
        self.trees[self.head + '^{tree}'] = 'e' * 40
        self.compare_base = merge
        self.assertTrue(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])
        self.remote_merge_tree = 'f' * 40
        self.assertFalse(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])
        self.remote_merge_tree = 'e' * 40
        self.detached_prs[0]['base']['repo']['full_name'] = 'other/one'
        self.assertFalse(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])

    def test_release_requires_real_inherited_lock_before_any_proof(self):
        path = self.state.parent / 'state-supervisor.lock'
        with path.open('a') as lock, patch('forge.human_wait._locked', side_effect=real_locked):
            result = release(self.config, self.state, 'one-build', lock_fd=lock.fileno(), run=self.fake_run, now=100)
            self.assertEqual(result['reason'], 'supervisor_lock_required')
            self.assertEqual(self.calls, [])
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = release(self.config, self.state, 'one-build', lock_fd=lock.fileno(), run=self.fake_run, now=100)
            self.assertTrue(result['released'], result)

    def test_conflicting_single_claim_identity_never_authorizes_release(self):
        for reference in [
            {'number':1, 'repository':{'nameWithOwner':'other/one'}, 'url':'https://github.com/o/one/issues/1'},
            {'number':1, 'repository':{'nameWithOwner':'o/one'}, 'url':'https://github.com/other/one/issues/1'},
            {'number':True, 'repository':{'nameWithOwner':'o/one'}}]:
            self.pr['closingIssuesReferences'] = [reference]
            self.assertFalse(self.decision()['eligible'])

    def test_builder_handoff_with_next_ready_requires_all_claims_handed_off(self):
        self.snapshot['counts']['builder'] = 1
        self.snapshot['schedule'].update(handed_off=[1], reserving=[])
        self.snapshot['work'].update(ready=[3], sentback=[], conflicts=[], limbo=[])
        self.save()
        self.assertTrue(self.decision()['eligible'])
        self.pr['headRefOid'] = 'f' * 40
        self.assertFalse(self.decision()['eligible'])
        self.pr['headRefOid'] = self.head
        for field, value in [('handed_off', []), ('reserving', [1])]:
            prior = self.snapshot['schedule'][field]
            self.snapshot['schedule'][field] = value
            self.save()
            self.assertEqual(self.decision()['reason'], 'role_has_work')
            self.snapshot['schedule'][field] = prior
        self.snapshot['work']['sentback'] = [2]
        self.save()
        self.assertFalse(self.decision()['eligible'])

    def test_completed_builder_next_ready_still_requires_exact_publication(self):
        self.completed_builder_setup()
        self.snapshot['counts']['builder'] = 1
        self.snapshot['schedule'].update(active=[], handed_off=[], reserving=[])
        self.snapshot['work']['ready'] = [3]
        self.save()
        self.assertTrue(self.decision()['eligible'])
        self.status = '?? unfinished'
        self.assertFalse(self.decision()['eligible'])
        self.assertFalse(any(c[:2] == ['tmux', 'kill-session'] for c in self.calls))

    def test_detached_older_commit_requires_final_pr_history_and_main_ancestry(self):
        self.completed_audit_setup()
        self.branch = None
        final_head, merge = 'c' * 40, 'd' * 40
        self.detached_prs = [{'number': 130, 'state': 'closed', 'merged_at': '2026-09-08',
            'head': {'sha': final_head}, 'base': {'repo': {'full_name': 'o/one'}}, 'merge_commit_sha': merge}]
        old_compare = 'ahead'
        def run(args, **kwargs):
            if '/compare/' + self.head + '...' + final_head in ' '.join(args):
                return SimpleNamespace(returncode=0, stdout=json.dumps({'status':old_compare,
                    'base_commit':{'sha':self.head},'behind_by':0}), stderr='')
            return self.fake_run(args, **kwargs)
        self.compare_base = merge
        result = check(self.config, self.state, 'one-audit', run=run, now=100)
        self.assertTrue(result['eligible'], result)
        self.assertEqual(result['evidence']['checkout']['final_head'], final_head)
        old_compare = 'diverged'
        self.assertFalse(check(self.config, self.state, 'one-audit', run=run, now=100)['eligible'])
        old_compare = 'ahead'
        self.compare_status = 'diverged'
        self.assertFalse(check(self.config, self.state, 'one-audit', run=run, now=100)['eligible'])
        self.compare_status = 'ahead'
        self.detached_prs.append(dict(self.detached_prs[0], number=131))
        self.assertFalse(check(self.config, self.state, 'one-audit', run=run, now=100)['eligible'])

    def test_detached_reviewer_reaches_completed_fallback(self):
        self.review_setup()
        self.head = 'd' * 40
        self.branch = None
        self.published_main = self.head
        self.compare_status = 'identical'
        result = check(self.config, self.state, 'one-review', run=self.fake_run, now=100)
        self.assertTrue(result['eligible'], result)

    def test_completed_auditor_ignores_only_explicitly_held_live_queue(self):
        self.completed_audit_setup()
        self.live_queue = [{'number':117, 'labels':[{'name':'hold'}, {'name':'ready-for-audit'}]}]
        self.assertTrue(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])
        self.live_queue[0]['labels'] = [{'name':'ready-for-audit'}]
        self.assertFalse(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])
        self.live_queue = [{'number':117}]
        self.assertFalse(check(self.config, self.state, 'one-audit', run=self.fake_run, now=100)['eligible'])

    def test_proof_deadline_clamps_remote_calls_and_preserves_worker(self):
        clock = [0]
        def slow(args, **kwargs):
            self.assertLessEqual(kwargs['timeout'], 30)
            result = self.fake_run(args, **kwargs)
            clock[0] += 31
            return result
        with patch('forge.handoff.time.monotonic', side_effect=lambda: clock[0]):
            result = release(self.config, self.state, 'one-build', run=slow, now=100)
        self.assertEqual(result['reason'], 'verification_deadline_exceeded')
        self.assertFalse(any(c[:2] == ['tmux','kill-session'] for c in self.calls))

    def test_successful_teardown_crossing_deadline_still_records_success(self):
        clock = [0]
        def run(args, **kwargs):
            result = self.fake_run(args, **kwargs)
            if args[:2] == ['tmux', 'kill-session']:
                clock[0] = 61
            return result
        with patch('forge.handoff.time.monotonic', side_effect=lambda: clock[0]):
            result = release(self.config, self.state, 'one-build', run=run, now=100)
        self.assertTrue(result['released'], result)
        receipt = json.loads((self.state / 'handoffs' / 'one-build.json').read_text())
        self.assertTrue(receipt['released'])

    def test_expired_proof_never_starts_teardown(self):
        from forge.handoff import bounded_run, release_command, Unverified
        clock = [0]
        with patch('forge.handoff.time.monotonic', side_effect=lambda: clock[0]):
            bounded = bounded_run(self.fake_run)
            clock[0] = 31
            with self.assertRaises(Unverified):
                release_command(['tmux','kill-session','-t','$1'], bounded, self.fake_run)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()


class ReviewerRejectionTests(unittest.TestCase):
    fake_run=HandoffTests.fake_run
    save=HandoffTests.save

    def setUp(self):
        HandoffTests.setUp(self)
        (self.config/'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_REVIEW={self.clone}\n')
        self.snapshot['counts']={'review':0}
        self.snapshot['work']={'review':[],'audit':[],'sentback':[2]}
        self.identity=self.identity.replace('one-build','one-review')
        self.pr.update(number=2,url='https://github.com/o/one/pull/2',labels=[{'name':'changes-requested'}],
            reviews=[{'id':'review-1','author':{'login':'forge-bot'},'state':'COMMENTED',
                      'commit':{'oid':self.head},'submittedAt':'1970-01-01T00:01:10Z',
                      'body':'Review of `'+self.head+'`: changes requested.'}])
        self.save()

    def decision(self):return check(self.config,self.state,'one-review',run=self.fake_run,now=100)

    def test_clean_named_and_detached_current_pr_head_can_release(self):
        for branch in ('issue-1-fix',None):
            self.branch=branch;result=self.decision()
            self.assertTrue(result['eligible'],result)
            self.assertEqual(result['evidence']['checkout_basis'],'current_pr_head_reviewer_rejection')
            self.assertEqual(result['evidence']['review_id'],'review-1')
            self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_old_generation_wrong_author_or_old_head_review_rejects(self):
        for key,value in [('submittedAt','1970-01-01T00:00:40Z'),('commit',{'oid':'d'*40}),('author',{'login':'other'})]:
            old=self.pr['reviews'][0][key];self.pr['reviews'][0][key]=value
            self.assertFalse(self.decision()['eligible']);self.pr['reviews'][0][key]=old

    def test_body_quote_or_approval_cannot_authorize_rejection(self):
        for body in ('Review passed.\nReview of `'+self.head+'`: changes requested.','All good.'):
            self.pr['reviews'][0]['body']=body
            self.assertFalse(self.decision()['eligible'])

    def test_conflicting_labels_dirty_work_and_child_jobs_are_preserved(self):
        self.pr['labels'].append({'name':'ready-for-audit'});self.assertFalse(self.decision()['eligible'])
        self.pr['labels']=[{'name':'changes-requested'}]
        self.status=' M file';self.assertFalse(self.decision()['eligible']);self.status=''
        self.processes+='\n12 11 npm';self.assertFalse(self.decision()['eligible'])

    def test_new_review_queue_and_unpublished_head_reject_release(self):
        self.live_queue=[{'number':3,'labels':[{'name':'ready-for-review'}]}]
        self.assertFalse(self.decision()['eligible']);self.live_queue=[]
        self.head='e'*40;self.assertFalse(self.decision()['eligible'])

    def test_replaced_generation_or_changed_pr_between_proofs_never_kills(self):
        self.change_on_second=lambda:self.pr['labels'].append({'name':'ready-for-review'})
        result=release(self.config,self.state,'one-review',run=self.fake_run,now=100)
        self.assertFalse(result['released']);self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_latest_review_supersedes_earlier_rejection(self):
        self.pr['reviews'].append({**self.pr['reviews'][0],'id':'review-2','submittedAt':'1970-01-01T00:01:20Z','state':'APPROVED','body':'Approved'})
        self.assertFalse(self.decision()['eligible'])

    def test_release_repeats_entire_proof_then_kills_only_verified_generation(self):
        result=release(self.config,self.state,'one-review',run=self.fake_run,now=100)
        self.assertTrue(result['released'],result)
        self.assertEqual(self.pr_reads,2)
        self.assertEqual([c for c in self.calls if c[:2]==['tmux','kill-session']],
                         [['tmux','kill-session','-t','$1']])

    def test_same_timestamp_conflicting_review_decisions_are_ambiguous(self):
        self.pr['reviews'].append({**self.pr['reviews'][0],'id':'review-2','state':'APPROVED'})
        self.assertFalse(self.decision()['eligible'])


class TrackedRoleCompletionTests(unittest.TestCase):
    fake_run=HandoffTests.fake_run
    save=HandoffTests.save

    def setUp(self):
        ReviewerRejectionTests.setUp(self)
        self.role='review'

    def assign(self,role='review'):
        from forge import workflow as w
        self.role=role
        (self.config/'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_{role.upper()}={self.clone}\n')
        self.identity=self.identity.replace('one-review','one-'+role)
        pr={'number':2,'head':self.head,'base':'c'*40}
        w.reconcile(self.state,'o/one','v1',60,[{'number':1,'stage':role,'prs':[pr]}],source_event_id='sync',now=60)
        a=w.reserve(self.state,'o/one',role,'one-'+role,'v1',[1],pr=pr,source_event_id='reserve',now=60)
        w.transition(self.state,a['id'],'bind',generation='$1|50|%1|10',provider='claude',source_event_id='bind',now=61)
        w.transition(self.state,a['id'],'submitted',generation='$1|50|%1|10',source_event_id='submit',now=65)
        self.assignment=a
        self.snapshot['counts'][role]=2
        self.snapshot['work'][role]=[3,4]
        self.save()

    def decision(self):return check(self.config,self.state,'one-'+self.role,run=self.fake_run,now=100)

    def merged(self):
        self.pr.update(state='MERGED',mergeCommit={'oid':'d'*40},mergedAt='1970-01-01T00:01:20Z',baseRefOid='b'*40)
        self.trees[self.head+'^{tree}']='e'*40
        self.compare_status='ahead';self.compare_base='d'*40

    def test_reviewer_rejection_ignores_unrelated_review_queue(self):
        self.assign();result=self.decision()
        self.assertTrue(result['eligible'],result)
        self.assertEqual(result['evidence']['assignment_id'],self.assignment['id'])
        self.assertFalse(any(c[:3]==['gh','pr','list'] for c in self.calls))

    def test_reviewer_pass_ignores_unrelated_review_queue(self):
        self.assign();self.pr['labels']=[{'name':'ready-for-audit'}]
        self.pr['reviews'][0]['body']='Reviewer: PASS'
        self.assertTrue(self.decision()['eligible'])

    def audit_record(self, outcome='FAIL', when='1970-01-01T00:01:20Z'):
        return {**self.pr['reviews'][0],'id':'audit-2','submittedAt':when,
                'body':'Security audit '+outcome+' — recorded outcome.\nHead: '+self.pr['headRefOid']+'\nBase: '+'c'*40}

    def test_emitted_reviewer_pass_heading_survives_later_same_author_audit(self):
        self.assign();self.pr['labels']=[{'name':'ready-for-audit'},{'name':'critical'}]
        for connector in ('at','for'):
            with self.subTest(connector=connector):
                self.pr['reviews'][0]['body']='Reviewer PASS '+connector+' '+self.head+' (issue #1).'
                self.pr['reviews']=self.pr['reviews'][:1]+[self.audit_record('PASS')]
                result=self.decision()
                self.assertTrue(result['eligible'],result)
                self.assertEqual(result['evidence']['review_id'],'review-1')
                self.assertEqual(result['evidence']['review_outcome'],'pass')
        self.assertEqual([x['name'] for x in self.pr['labels']],['ready-for-audit','critical'])

    def test_completed_review_pass_survives_verified_downstream_audit_failure(self):
        self.assign();self.pr['reviews'][0]['body']='Reviewer PASS at '+self.head+' for #1.'
        self.pr['reviews'].append(self.audit_record())
        result=self.decision();self.assertTrue(result['eligible'],result)
        self.assertEqual(result['evidence']['review_outcome'],'pass')
        self.assertEqual(result['evidence']['downstream_audit_id'],'audit-2')
        self.assertEqual([x['name'] for x in self.pr['labels']],['changes-requested'])
        for changes in ({'commit':{'oid':'d'*40}}, {'author':{'login':'someone-else'}},
                        {'submittedAt':'1970-01-01T00:01:05Z'}, {'body':'Security audit FAIL without exact revisions'}):
            previous=self.pr['reviews'][1].copy();self.pr['reviews'][1].update(changes)
            self.assertFalse(self.decision()['eligible'],changes)
            self.pr['reviews'][1]=previous

    def test_latest_role_rejection_is_not_hidden_by_old_pass_or_other_role(self):
        self.assign();self.pr['labels']=[{'name':'ready-for-audit'}]
        self.pr['reviews'][0]['body']='Reviewer PASS for '+self.head
        self.pr['reviews'].append({**self.pr['reviews'][0],'id':'review-new','submittedAt':'1970-01-01T00:01:15Z','body':'Reviewer FAIL: blocking defect'})
        self.pr['reviews'].append(self.audit_record('PASS'))
        self.assertFalse(self.decision()['eligible'])

    def test_arbitrary_prose_quotes_and_outcome_prefixes_are_not_result_headings(self):
        self.assign();self.pr['labels']=[{'name':'ready-for-audit'}]
        for body in ('Notes: Reviewer PASS for '+self.head, '> Reviewer PASS for '+self.head,
                     'Reviewer PASSING checks now','Discussion\nReviewer PASS for '+self.head):
            self.pr['reviews'][0]['body']=body
            self.assertFalse(self.decision()['eligible'],body)

    def test_exact_audit_failure_can_release_clean_canonically_published_stale_main(self):
        self.assign('audit');self.pr['reviews']=[self.audit_record()]
        self.head='d'*40;self.branch='main';self.compare_status='ahead'
        result=self.decision();self.assertTrue(result['eligible'],result)
        self.assertEqual(result['evidence']['checkout_basis'],'assigned_audit_result_with_published_main_checkout')
        self.assertEqual(result['evidence']['pr_head'],'a'*40)
        self.assertEqual(result['evidence']['head'],'d'*40)
        self.assertTrue(any('/compare/'+self.head+'...'+self.published_main in str(call) for call in self.calls))
        result=release(self.config,self.state,'one-audit',run=self.fake_run,now=100)
        self.assertTrue(result['released'],result)
        self.assertEqual([call for call in self.calls if call[:2]==['tmux','kill-session']],
                         [['tmux','kill-session','-t','$1']])

    def test_archive_audit_does_not_release_unpublished_or_modified_checkout(self):
        self.assign('audit');self.pr['reviews']=[self.audit_record()]
        self.head='d'*40;self.branch='main';self.compare_status='ahead'
        for attribute,value in (('branch','local-unpublished-work'),('branch',None),('status','?? artifact'),
                                ('compare_status','diverged'),('compare_base','b'*40),('published_main','invalid')):
            before=getattr(self,attribute);setattr(self,attribute,value)
            self.assertFalse(self.decision()['eligible'],(attribute,value))
            setattr(self,attribute,before)
        self.pr['reviews'][0]['body']='Audit FAIL in prose without exact head/base'
        self.assertFalse(self.decision()['eligible'])

    def test_auditor_merged_completion_survives_base_advance(self):
        self.assign('audit');self.merged();self.branch=None
        result=self.decision();self.assertTrue(result['eligible'],result)
        self.assertEqual(result['evidence']['checkout_basis'],'assigned_merged_pr_on_main')

    def test_reviewer_merged_completion_with_other_work(self):
        self.assign();self.merged();self.assertTrue(self.decision()['eligible'])

    def test_audit_rejection_requires_actual_current_candidate_audit(self):
        self.assign('audit')
        self.assertFalse(self.decision()['eligible'])
        self.pr['reviews'][0]['body']='Security audit: FAIL\nHead: '+self.head+'\nBase: '+'c'*40
        self.assertTrue(self.decision()['eligible'])
        self.pr['reviews'][0]['body']='Security audit: FAIL\nHead: '+self.head
        self.assertFalse(self.decision()['eligible'])

    def test_exact_generation_head_base_links_and_author_are_required(self):
        self.assign()
        for key,value in [('headRefOid','invalid'),('baseRefOid','invalid'),('author',{'login':'other'}),('closingIssuesReferences',[{'number':8,'repository':{'nameWithOwner':'o/one'}}])]:
            old=self.pr[key];self.pr[key]=value
            self.assertFalse(self.decision()['eligible']);self.pr[key]=old
        self.identity=self.identity.replace('$1','$2');self.assertFalse(self.decision()['eligible'])

    def test_dirty_child_work_and_held_target_are_not_released(self):
        self.assign();self.status=' M important';self.assertFalse(self.decision()['eligible']);self.status=''
        self.processes+='\n12 11 npm';self.assertFalse(self.decision()['eligible'])
        self.processes=self.processes.rsplit('\n',1)[0]
        self.pr['labels'].append({'name':'hold'});self.assertFalse(self.decision()['eligible'])

    def test_wrong_latest_review_or_old_review_cannot_complete_assignment(self):
        self.assign();self.pr['reviews'][0]['submittedAt']='1970-01-01T00:01:00Z'
        self.assertFalse(self.decision()['eligible'])
        self.pr['reviews'][0]['submittedAt']='1970-01-01T00:01:10Z'
        self.pr['reviews'].append({**self.pr['reviews'][0],'id':'new','submittedAt':'1970-01-01T00:01:20Z','body':'Reviewer: PASS'})
        self.assertFalse(self.decision()['eligible'])

    def test_merged_tree_or_main_ancestry_mismatch_preserves_worker(self):
        self.assign('audit');self.merged();self.remote_merge_tree='f'*40
        self.assertFalse(self.decision()['eligible']);self.remote_merge_tree='e'*40
        self.compare_status='diverged';self.assertFalse(self.decision()['eligible'])

    def test_double_proof_change_never_releases(self):
        self.assign();self.change_on_second=lambda:self.pr['labels'].append({'name':'ready-for-review'})
        result=release(self.config,self.state,'one-review',run=self.fake_run,now=100)
        self.assertFalse(result['released']);self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_open_handoff_base_advance_retires_only_old_execution(self):
        self.assign();self.pr['baseRefOid']='f'*40
        result=self.decision();self.assertTrue(result['eligible'],result)
        self.assertEqual(result['evidence']['assigned_base'],'c'*40)
        self.assertEqual(result['evidence']['pr_base'],'f'*40)


    def test_explicit_result_heading_and_historical_rejection(self):
        self.assign()
        self.pr['reviews'][0]['body']='Reviewer result: changes requested for '+self.head
        self.assertTrue(self.decision()['eligible'])
        self.pr['headRefOid']='f'*40
        self.pr['labels']=[{'name':'ready-for-review'}]
        self.pr['reviews'].append({**self.pr['reviews'][0], 'id':'new-revision',
            'commit':{'oid':'f'*40}, 'submittedAt':'1970-01-01T00:01:20Z', 'body':'Reviewer PASS'})
        result=self.decision();self.assertTrue(result['eligible'],result)
        self.assertEqual(result['evidence']['assigned_head'],self.head)
        self.assertEqual(result['evidence']['pr_head'],'f'*40)
        self.assertEqual(result['evidence']['review_id'],'review-1')
        self.assertEqual(result['evidence']['checkout_basis'],'assigned_historical_pr_head_rejection')
        self.pr['reviews'][0]['body']='Reviewer result: PASS'
        self.assertFalse(self.decision()['eligible'])

    def test_result_heading_rejects_prose_and_unbound_old_revision(self):
        self.assign();self.pr['headRefOid']='f'*40
        for body in ['Reviewer result might be changes requested', 'The Reviewer result: changes requested',
                     '> Reviewer result: changes requested', 'Reviewer result: changes requestedness']:
            self.pr['reviews'][0]['body']=body
            self.assertFalse(self.decision()['eligible'],body)
        self.pr['reviews'][0]['body']='Reviewer result: changes requested'
        self.pr['reviews'][0]['commit']['oid']='e'*40
        self.assertFalse(self.decision()['eligible'])

    def test_historical_audit_rejection_requires_exact_assigned_revisions(self):
        self.assign('audit');self.pr['reviews']=[self.audit_record()]
        self.pr['headRefOid']='f'*40;self.pr['labels']=[{'name':'ready-for-review'}]
        result=self.decision();self.assertTrue(result['eligible'],result)
        self.pr['reviews'][0]['body']='Security audit result: FAIL on '+self.head
        self.assertFalse(self.decision()['eligible'])


class StructuredRoleHandoffTests(unittest.TestCase):
    fake_run=HandoffTests.fake_run
    save=HandoffTests.save
    setUp=TrackedRoleCompletionTests.setUp
    assign=TrackedRoleCompletionTests.assign
    decision=TrackedRoleCompletionTests.decision
    audit_record=TrackedRoleCompletionTests.audit_record

    def structured(self,role='review',outcome='fail',**changes):
        record={'schema':1,'role':role,'outcome':outcome,'head':self.assignment['pr']['head'],
                'base':self.assignment['pr']['base'],'assignment_id':self.assignment['id']}
        record.update(changes)
        return 'Evidence is published below.\nFORGE_RESULT: '+json.dumps(record)

    def test_exact_structured_review_pass_and_fail_with_all_checkout_proofs(self):
        self.assign()
        for outcome,stage in [('fail','changes-requested'),('pass','ready-for-audit')]:
            self.pr['labels']=[{'name':stage}]
            self.pr['reviews'][0]['body']=self.structured(outcome=outcome)
            result=self.decision();self.assertTrue(result['eligible'],result)
            self.assertEqual(result['evidence']['review_outcome'],outcome)
        self.status=' M unfinished'
        self.assertFalse(self.decision()['eligible'])

    def test_structured_audit_failure_binds_exact_assignment_and_revisions(self):
        self.assign('audit');self.pr['reviews']=[self.audit_record()]
        self.pr['reviews'][0]['body']=self.structured('audit')
        self.assertTrue(self.decision()['eligible'])
        self.pr['reviews'][0]['body']=self.structured('review')
        self.assertFalse(self.decision()['eligible'])

    def test_invalid_or_wrong_binding_cannot_fall_back_to_legacy_fail_heading(self):
        self.assign()
        for marker in ('FORGE_RESULT: broken',self.structured(head='d'*40),
                       self.structured(base='d'*40),
                       self.structured(assignment_id='11111111-1111-4111-8111-111111111111'),
                       self.structured()+'\n'+self.structured()):
            self.pr['reviews'][0]['body']='Reviewer FAIL: blocking defect\n'+marker
            result=self.decision();self.assertFalse(result['eligible'],marker)
            self.assertEqual(result['reason'],'structured_role_result_invalid')

    def test_structured_result_conflicting_with_native_review_is_not_accepted(self):
        self.assign();self.pr['reviews'][0]['body']=self.structured()
        self.pr['reviews'][0]['state']='APPROVED'
        self.assertEqual(self.decision()['reason'],'structured_role_result_state_conflict')

    def test_other_role_marker_does_not_borrow_current_assignment_identity(self):
        self.assign();self.pr['reviews'][0]['body']=self.structured()
        other={**self.pr['reviews'][0],'id':'audit-other',
               'body':self.structured('audit',assignment_id='11111111-1111-4111-8111-111111111111')}
        self.pr['reviews'].append(other)
        self.assertTrue(self.decision()['eligible'])

    def test_actual_legacy_outcome_and_exact_head_forms_remain_anchored(self):
        self.assign()
        for body in ('Review outcome: changes requested for head '+self.head,
                     'Review of exact head '+self.head+': changes requested'):
            self.pr['reviews'][0]['body']=body
            self.assertTrue(self.decision()['eligible'],body)
            self.pr['reviews'][0]['body']='Notes: '+body
            self.assertFalse(self.decision()['eligible'],body)


    def test_same_head_retry_ignores_valid_earlier_assignment_result(self):
        self.assign()
        current={**self.pr['reviews'][0],'id':'current-result',
                 'submittedAt':'1970-01-01T00:01:10Z','body':self.structured()}
        earlier={**current,'id':'earlier-result','submittedAt':'1970-01-01T00:01:04Z',
                 'body':self.structured(assignment_id='11111111-1111-4111-8111-111111111111')}
        self.pr['reviews']=[earlier,current]
        result=self.decision();self.assertTrue(result['eligible'],result)
        self.assertEqual(result['evidence']['review_id'],'current-result')
        self.pr['reviews']=[earlier]
        result=self.decision();self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'],'assigned_step_record_missing')

    def test_same_head_current_wrong_assignment_or_malformed_record_still_refuses(self):
        self.assign()
        current={**self.pr['reviews'][0],'body':self.structured()}
        earlier={**current,'id':'earlier-result','submittedAt':'1970-01-01T00:01:04Z',
                 'body':self.structured(assignment_id='11111111-1111-4111-8111-111111111111')}
        for body in ('FORGE_RESULT: broken',
                     self.structured(assignment_id='22222222-2222-4222-8222-222222222222')):
            self.pr['reviews']=[earlier,{**current,'body':body}]
            result=self.decision();self.assertFalse(result['eligible'])
            self.assertEqual(result['reason'],'structured_role_result_invalid')
