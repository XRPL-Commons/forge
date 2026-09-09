from tests.host_isolation import deny_host_tmux
import fcntl
import json
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import patch

from forge.human_wait import check, release, _locked
from forge.observability import connect


class HumanWaitTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / 'state'
        self.config = root / 'config'
        self.config.mkdir()
        (self.config / 'app.repo').write_text('REPO=owner/app\nOWNER_LABEL=auto:owned-by-luc\n')
        self.head, self.base = 'a' * 40, 'b' * 40
        body = f'## Security audit PASS\nCriticality: CRITICAL\nHead {self.head}\nBase {self.base}\nAwaiting human approval.'
        self.audit = {'kind': 'audit', 'body': body, 'commit_id': self.head,
                      'created_at': '2026-09-07T00:00:00Z', 'url': 'https://github.com/owner/app/pull/10#pullrequestreview-7'}
        data = {'number': 10, 'state': 'open', 'head_sha': self.head, 'base_sha': self.base,
                'evidence': [self.audit], 'linked_issues': [{'repo': 'owner/app', 'number': 1}]}
        db = connect(self.state, write=True)
        with db:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?)', ('owner/app', 10, 'pr', 'audit', '', '', '', json.dumps(data)))
        db.close()
        self.live = {'number': 10, 'state': 'OPEN', 'isDraft': False, 'headRefOid': self.head, 'baseRefOid': self.base,
                     'labels': [{'name': 'critical'}, {'name': 'ready-for-audit'}],
                     'author': {'login': 'forge-bot'}, 'reviewDecision': 'REVIEW_REQUIRED',
                     'closingIssuesReferences': [{'number': 1, 'url': 'https://github.com/owner/app/issues/1'}]}
        self.reviews = [{'id': 7, 'state': 'COMMENTED', 'user': {'login': 'forge-bot'},
                         'body': body, 'submitted_at': self.audit['created_at'], 'commit_id': self.head,
                         'html_url': self.audit['url']}]
        self.inventory = [{'number': 10, 'pull_request': {}}]
        self.calls = []

    def fake_run(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ['gh', 'pr', 'view']:
            value = self.live
        elif args[-1].endswith('/labels?per_page=100'):
            value = [[{'name': 'auto:owned-by-luc'}]]
        elif '/issues?' in args[-1]:
            value = [self.inventory]
        elif '/reviews?' in args[-1]:
            value = [self.reviews]
        else:
            raise AssertionError(args)
        return CompletedProcess(args, 0, json.dumps(value), '')

    def check(self):
        return check(self.config, self.state, 'owner/app', run=self.fake_run)

    def test_all_audits_waiting_is_eligible_without_writes(self):
        result = self.check()
        self.assertTrue(result['eligible'], result)
        self.assertEqual(result['evidence']['candidates'][0]['issues'], [1])
        self.assertTrue(all(args[:2] in (['gh', 'api'], ['gh', 'pr']) for args in self.calls))

    def test_fresh_audit_is_primary_when_history_missing_or_stale(self):
        db = connect(self.state, write=True)
        with db:
            db.execute('DELETE FROM items')
        db.close()
        self.assertTrue(self.check()['eligible'])
        for path in self.state.glob('observability.sqlite3*'):
            path.unlink()
        self.assertTrue(self.check()['eligible'])
        self.assertFalse((self.state / 'observability.sqlite3').exists())

    def test_fresh_proof_requires_correct_pr_and_canonical_current_links(self):
        self.live['number'] = 11
        self.assertFalse(self.check()['eligible'])
        self.live['number'] = 10
        self.live['closingIssuesReferences'] = []
        self.assertEqual(self.check()['reason'], 'current_issue_links_unavailable')

    def test_stale_cached_pass_never_overrides_fresh_failed_audit(self):
        self.reviews.append({**self.reviews[0], 'id': 8, 'submitted_at': '2026-09-08T00:00:00Z',
                             'body': self.audit['body'].replace('PASS', 'FAIL')})
        self.assertFalse(self.check()['eligible'])

    def test_fresh_proof_uses_existing_four_remote_reads(self):
        self.assertTrue(self.check()['eligible'])
        self.assertEqual(len(self.calls), 4)

    def test_empty_aggregate_still_honors_recorded_critical_policy(self):
        for decision in ('', None):
            with self.subTest(decision=decision):
                self.live['reviewDecision'] = decision
                self.assertTrue(self.check()['eligible'])

    def test_empty_aggregate_never_hides_human_approval_or_changes(self):
        self.live['reviewDecision'] = ''
        self.reviews.append({'state': 'APPROVED', 'commit_id': self.head, 'user': {'login': 'human'}})
        self.assertEqual(self.check()['reason'], 'approval_requires_recheck')
        self.reviews[-1]['state'] = 'CHANGES_REQUESTED'
        self.assertEqual(self.check()['reason'], 'review_requires_recheck')

    def test_empty_aggregate_still_requires_current_authored_pass(self):
        self.live['reviewDecision'] = ''
        self.reviews[0]['body'] = self.reviews[0]['body'].replace('PASS', 'FAIL')
        self.assertFalse(self.check()['eligible'])
        self.reviews[0]['body'] = self.audit['body']
        self.reviews[0]['user']['login'] = 'untrusted'
        self.assertFalse(self.check()['eligible'])

    def test_unexpected_or_missing_aggregate_never_suppresses_auditor(self):
        for decision in ('APPROVED', 'CHANGES_REQUESTED', 'UNKNOWN', 'unexpected'):
            with self.subTest(decision=decision):
                self.live['reviewDecision'] = decision
                self.assertFalse(self.check()['eligible'])
        del self.live['reviewDecision']
        self.assertFalse(self.check()['eligible'])

    def test_explicit_auditor_pass_heading_is_verified_fresh(self):
        body = self.audit['body'].replace('Security audit PASS', 'Auditor: PASS')
        self.reviews[0]['body'] = body
        self.assertTrue(self.check()['eligible'])

    def test_structured_pass_accepts_natural_heading_and_rejects_changed_record(self):
        record = {'schema': 1, 'role': 'audit', 'outcome': 'pass', 'head': self.head,
                  'base': self.base, 'assignment_id': '11111111-1111-4111-8111-111111111111'}
        body = 'PASS: no blocking findings.'
        def publish(value):self.reviews[0]['body'] = body + '\nFORGE_RESULT: ' + json.dumps(value)
        publish(record)
        self.assertTrue(self.check()['eligible'])
        for key, value in [('outcome', 'fail'), ('head', 'c'*40), ('base', 'c'*40),
                           ('role', 'review'), ('schema', 2)]:
            with self.subTest(key=key):
                publish({**record, key: value})
                self.assertFalse(self.check()['eligible'])
        publish(record)
        self.reviews[0]['body'] += '\nFORGE_RESULT: ' + json.dumps(record)
        self.assertFalse(self.check()['eligible'])

    def test_structured_pass_preserves_authorship_and_human_approval_gates(self):
        record = {'schema': 1, 'role': 'audit', 'outcome': 'pass', 'head': self.head,
                  'base': self.base, 'assignment_id': '11111111-1111-4111-8111-111111111111'}
        self.reviews[0]['body'] = self.audit['body'] + '\nFORGE_RESULT: ' + json.dumps(record)
        self.reviews[0]['user']['login'] = 'untrusted'
        self.assertFalse(self.check()['eligible'])
        self.reviews[0]['user']['login'] = 'forge-bot'
        self.reviews.append({'state': 'APPROVED', 'commit_id': self.head, 'user': {'login': 'human'}})
        self.assertEqual(self.check()['reason'], 'approval_requires_recheck')

    def test_changed_head_or_base_resumes_auditing(self):
        for key in ('headRefOid', 'baseRefOid'):
            with self.subTest(key=key):
                before = self.live[key]
                self.live[key] = 'c' * 40
                self.assertFalse(self.check()['eligible'])
                self.live[key] = before

    def test_human_approval_resumes_even_if_more_required(self):
        self.reviews.append({'state': 'APPROVED', 'commit_id': self.head, 'user': {'login': 'human'}})
        self.assertEqual(self.check()['reason'], 'approval_requires_recheck')

    def test_shared_identity_does_not_prove_human_approval(self):
        self.reviews.append({'state': 'APPROVED', 'commit_id': self.head, 'user': {'login': 'forge-bot'}})
        self.assertTrue(self.check()['eligible'])

    def test_additional_actionable_pr_prevents_suppression(self):
        self.inventory.append({'number': 11, 'pull_request': {}})
        self.assertFalse(self.check()['eligible'])

    def test_changed_audit_same_head_resumes_auditing(self):
        self.reviews[0]['body'] = self.reviews[0]['body'].replace('PASS', 'FAIL')
        self.assertFalse(self.check()['eligible'])

    def test_empty_queue_and_network_failure_do_not_suppress(self):
        self.inventory.clear()
        self.assertFalse(self.check()['eligible'])
        result = check(self.config, self.state, 'owner/app', run=lambda *a, **k: CompletedProcess(a, 1, '', ''))
        self.assertFalse(result['eligible'])

    def tracked_auditor(self):
        import time
        from forge import workflow as w
        self.clone=self.state.parent/'audit-clone'
        with (self.config/'app.repo').open('a') as f:f.write('CLONE_AUDIT='+str(self.clone)+'\n')
        now=time.time();self.generation='$7|100|%7|700'
        pr={'number':10,'head':self.head,'base':self.base}
        w.reconcile(self.state,'owner/app','v1',now,[{'number':1,'stage':'audit','prs':[pr]}],source_event_id='sync',now=now)
        a=w.reserve(self.state,'owner/app','audit','app-audit','v1',[1],pr=pr,source_event_id='reserve',now=now)
        w.transition(self.state,a['id'],'bind',generation=self.generation,provider='claude',source_event_id='bind',now=now)
        w.transition(self.state,a['id'],'submitted',generation=self.generation,source_event_id='submit',now=now)
        w.reconcile(self.state,'owner/app','v2',now+0.001,[{'number':1,'stage':'audit','disposition':'critical',
                    'reason':'human_approval_required','prs':[pr]}],source_event_id='critical',now=now+0.001)
        self.inventory.append({'number':11,'pull_request':{}})
        self.dirty=False;self.busy=False;self.replaced=False;self.killed=[]
        return a

    def scoped_run(self,args,**kwargs):
        if args[:3]==['gh','pr','view'] and args[3]=='11':
            self.calls.append(args)
            return CompletedProcess(args,0,json.dumps({**self.live,'number':11,'labels':[{'name':'ready-for-audit'}]}),'')
        if args[0]=='gh':return self.fake_run(args,**kwargs)
        self.calls.append(args)
        if args[0]=='git':
            if args[-2:]==['rev-parse','--show-toplevel']:value=str(self.clone)
            elif args[-3:]==['remote','get-url','origin']:value='https://github.com/owner/app.git'
            elif 'status' in args:value=' M pending.txt' if self.dirty else ''
            elif args[-2:]==['rev-parse','HEAD']:value=self.head
            else:raise AssertionError(args)
        elif args[:2]==['tmux','list-panes']:
            value='app-audit|$8|200|%8|800|claude' if self.replaced else 'app-audit|$7|100|%7|700|claude'
        elif args[:2]==['tmux','capture-pane']:
            value='Working · esc to interrupt' if self.busy else 'Done.\n❯\n bypass permissions on'
        elif args[:2]==['tmux','kill-session']:
            self.killed.append(args);value=''
        elif args[0]=='ps':
            value='800 1 bash\n801 800 claude' if self.replaced else '700 1 bash\n701 700 claude'
        else:raise AssertionError(args)
        return CompletedProcess(args,0,value,'')

    def scoped_release(self):
        lock=self.state.parent/'state-supervisor.lock'
        with lock.open('w') as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            return release(self.config,self.state,'owner/app',session='app-audit',lock_fd=f.fileno(),run=self.scoped_run)

    def test_exact_audit_wait_releases_with_another_actionable_pr_and_retires_only_assignment(self):
        from forge import workflow as w
        a=self.tracked_auditor()
        global_wait=check(self.config,self.state,'owner/app',run=self.scoped_run)
        self.assertFalse(global_wait['eligible'])
        self.calls.clear()
        result=self.scoped_release()
        self.assertTrue(result['released'],result)
        self.assertEqual(result['evidence']['assignment_id'],a['id'])
        self.assertEqual([p['pr'] for p in result['evidence']['candidates']],[10])
        self.assertEqual(self.killed,[['tmux','kill-session','-t','$7']])
        self.assertEqual([args[3] for args in self.calls if args[:3]==['gh','pr','view']],['10','10'])
        view=w.read_state(self.state)
        self.assertEqual(view['assignments'][0]['state'],'retired')
        self.assertEqual(view['slots'][0]['state'],'free')
        self.assertEqual((view['issues'][0]['stage'],view['issues'][0]['disposition']),('audit','critical'))

    def test_scoped_wait_never_releases_dirty_busy_replaced_or_changed_target(self):
        self.tracked_auditor()
        for flag in ('dirty','busy','replaced'):
            with self.subTest(flag=flag):
                setattr(self,flag,True)
                result=self.scoped_release()
                self.assertFalse(result['released'],result);self.assertEqual(self.killed,[])
                setattr(self,flag,False)
        self.live['closingIssuesReferences']=[{'number':2,'url':'https://github.com/owner/app/issues/2'}]
        self.assertEqual(self.scoped_release()['reason'],'audit_assignment_target_changed')
        self.assertEqual(self.killed,[])

    def test_scoped_wait_rejects_new_head_even_when_new_audit_is_valid(self):
        self.tracked_auditor()
        self.live['headRefOid']='c'*40
        self.reviews[0]['commit_id']='c'*40
        self.reviews[0]['body']=self.reviews[0]['body'].replace(self.head,'c'*40)
        self.assertEqual(self.scoped_release()['reason'],'audit_assignment_target_changed')
        self.assertEqual(self.killed,[])

    def test_release_requires_inherited_supervisor_lock(self):
        result = release(self.config, self.state, 'owner/app', lock_fd=-1, run=self.fake_run)
        self.assertFalse(result['released'])
        self.assertEqual(self.calls, [])
        lock = self.state.parent / 'state-supervisor.lock'
        with lock.open('w') as file:
            self.assertFalse(_locked(self.state, file.fileno()))
            fcntl.flock(file, fcntl.LOCK_EX)
            self.assertTrue(_locked(self.state, file.fileno()))

    def test_release_checks_twice_and_targets_exact_id(self):
        evidence = {'repo': 'owner/app', 'role': 'audit', 'session_id': '$7', 'candidates': []}
        with patch('forge.human_wait._locked', return_value=True), patch('forge.human_wait._release_evidence', return_value=evidence) as checks, patch('forge.human_wait.release_command') as command:
            result = release(self.config, self.state, 'owner/app', session='app-audit', run=self.fake_run)
        self.assertTrue(result['released'])
        self.assertEqual(checks.call_count, 2)
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0], ['tmux', 'kill-session', '-t', '$7'])
        self.assertTrue((self.state / 'human-waits/app-audit.json').exists())

    def test_changed_session_is_never_killed(self):
        with patch('forge.human_wait._locked', return_value=True), patch('forge.human_wait._release_evidence', side_effect=[{'session_id': '$7'}, {'session_id': '$8'}]), patch('forge.human_wait.release_command') as command:
            result = release(self.config, self.state, 'owner/app', session='app-audit', run=self.fake_run)
        self.assertFalse(result['released'])
        command.assert_not_called()

    def test_forged_or_dismissed_audit_cannot_suppress_work(self):
        self.reviews[0]['user']['login'] = 'untrusted-commenter'
        self.assertEqual(self.check()['reason'], 'audit_evidence_changed')
        self.reviews[0]['user']['login'] = 'forge-bot'
        self.reviews[0]['state'] = 'DISMISSED'
        self.assertEqual(self.check()['reason'], 'audit_evidence_changed')

    def test_verified_release_clears_only_its_no_progress_cooldown(self):
        from forge.control import atomic_json
        path = self.state / 'legacy-health' / 'guard.json'
        original = {'account_until': 9999, 'account_reason': 'blocked_quota', 'sessions': {
            'app-audit': {'reason': 'no_progress', 'until': 9000, 'launches': [1, 2]},
            'other-audit': {'reason': 'no_progress', 'until': 8000, 'launches': [3]}}}
        atomic_json(path, original)
        evidence = {'repo': 'owner/app', 'role': 'audit', 'session_id': '$7', 'candidates': []}
        with patch('forge.human_wait._locked', return_value=True), patch('forge.human_wait._release_evidence', return_value=evidence), patch('forge.human_wait.release_command'):
            result = release(self.config, self.state, 'owner/app', session='app-audit', run=self.fake_run)
        self.assertTrue(result['released'])
        self.assertTrue(result['cleared_no_progress'])
        expected = json.loads(json.dumps(original))
        expected['sessions']['app-audit'] = {'until': 0, 'launches': [1, 2]}
        self.assertEqual(json.loads(path.read_text()), expected)

    def test_verified_wait_preserves_auth_quota_permission_and_launch_guards(self):
        from forge.control import atomic_json
        from forge.human_wait import _clear_verified_no_progress
        path = self.state / 'legacy-health' / 'guard.json'
        for reason in ('blocked_auth', 'blocked_quota', 'blocked_permission', 'launch_budget_exhausted'):
            original = {'account_until': 9999, 'sessions': {'app-audit': {'reason': reason, 'until': 9000, 'launches': [1]}}}
            atomic_json(path, original)
            self.assertFalse(_clear_verified_no_progress(self.state, 'app-audit'))
            self.assertEqual(json.loads(path.read_text()), original)

    def test_failed_release_never_clears_no_progress(self):
        from forge.control import atomic_json
        from forge.handoff import Unverified
        path = self.state / 'legacy-health' / 'guard.json'
        original = {'sessions': {'app-audit': {'reason': 'no_progress', 'until': 9000}}}
        atomic_json(path, original)
        evidence = {'repo': 'owner/app', 'role': 'audit', 'session_id': '$7', 'candidates': []}
        with patch('forge.human_wait._locked', return_value=True), patch('forge.human_wait._release_evidence', return_value=evidence), patch('forge.human_wait.release_command', side_effect=Unverified('kill_failed')):
            result = release(self.config, self.state, 'owner/app', session='app-audit', run=self.fake_run)
        self.assertFalse(result['released'])
        self.assertEqual(json.loads(path.read_text()), original)

    def test_latest_check_receipt_failure_invalidates_success(self):
        import hashlib
        from forge.human_wait import write_check_receipt
        repo = 'Owner/App'
        write_check_receipt(self.state, repo, {'eligible': True, 'reason': 'verified_awaiting_human_approval',
                                             'evidence': {'repo': repo, 'candidates': []}}, now=1)
        value = write_check_receipt(self.state, repo, {'eligible': False, 'reason': 'approval_requires_recheck',
                                                     'evidence': {}}, now=2)
        path = self.state / 'human-wait-checks' / (hashlib.sha256(repo.lower().encode()).hexdigest() + '.json')
        self.assertEqual(json.loads(path.read_text()), value)
        self.assertFalse(value['eligible'])
        self.assertEqual(value['observed_at'], 2)

    def test_library_check_never_creates_receipt_and_oversize_fails_closed(self):
        from forge.human_wait import write_check_receipt
        self.assertTrue(self.check()['eligible'])
        self.assertFalse((self.state / 'human-wait-checks').exists())
        value = write_check_receipt(self.state, 'owner/app', {'eligible': True, 'evidence': {'extra': 'x' * 65536}})
        self.assertFalse(value['eligible'])
        self.assertEqual(value['reason'], 'check_receipt_limit')

    def historical_check(self, *, tree='same-tree', merge_tree='same-tree', ancestor=True, pr_head=None, merged=True):
        from forge.human_wait import _published_checkout, Unverified
        old, merge = 'd' * 40, 'e' * 40
        def run(args, **kwargs):
            if 'symbolic-ref' in args:
                output = 'audit-94'
            elif args[-1] == 'branch.audit-94.remote':
                output = 'origin'
            elif args[-1] == 'branch.audit-94.merge':
                output = 'refs/heads/issue-93'
            elif args[:3] == ['gh', 'pr', 'list']:
                output = json.dumps([{'number': 94, 'headRefOid': pr_head or old, 'headRefName': 'issue-93',
                                      'mergeCommit': {'oid': merge}, 'state': 'MERGED' if merged else 'OPEN',
                                      'mergedAt': '2026-09-04'}])
            elif args[-1] == old + '^{tree}':
                output = tree
            elif args[-1] == merge + '^{tree}':
                output = merge_tree
            elif 'merge-base' in args:
                return CompletedProcess(args, 0 if ancestor else 1, '', '')
            else:
                raise AssertionError(args)
            return CompletedProcess(args, 0, output, '')
        return _published_checkout(['git', '-C', '/clone'], 'owner/app', old, {self.base}, run)

    def test_old_checkout_requires_exact_merged_tree_and_current_base_ancestry(self):
        proof = self.historical_check()
        self.assertEqual(proof['pr'], 94)
        self.assertEqual(proof['branch'], 'audit-94')
        self.assertEqual(proof['remote_branch'], 'issue-93')
        self.assertEqual(proof['verified_bases'], [self.base])
        from forge.handoff import Unverified
        for changes in ({'merge_tree': 'different'}, {'ancestor': False},
                        {'pr_head': 'f' * 40}, {'merged': False}):
            with self.subTest(changes=changes), self.assertRaises(Unverified):
                self.historical_check(**changes)

    def test_detached_historical_audit_requires_merged_publication_and_all_bases(self):
        from forge.human_wait import _published_checkout
        from forge.handoff import Unverified
        proof = {'basis':'exact_tree_of_verified_merged_pr','merge':'d'*40,'head':'e'*40,'branch':None}
        with patch('forge.handoff._checkout_branch', return_value=None), \
             patch('forge.handoff._detached_merged_checkout', return_value=proof) as published, \
             patch('forge.human_wait._canonical_ancestor') as ancestor:
            result = _published_checkout(['git','-C','/clone'], 'owner/app', proof['head'], {self.base}, self.fake_run)
            self.assertEqual(result['verified_bases'], [self.base])
            published.assert_called_once()
            ancestor.assert_called_once_with(['git','-C','/clone'], 'owner/app', proof['merge'], self.base, self.fake_run)
            ancestor.side_effect = Unverified('head_not_verified_base_ancestor')
            with self.assertRaises(Unverified):
                _published_checkout(['git','-C','/clone'], 'owner/app', proof['head'], {self.base}, self.fake_run)
            with self.assertRaises(Unverified):
                _published_checkout(['git','-C','/clone'], 'owner/app', proof['head'], set(), self.fake_run)

    def test_old_main_requires_ancestry_to_every_fresh_candidate_base(self):
        from forge.human_wait import _published_checkout
        from forge.handoff import Unverified
        head, first_base, second_base = 'a' * 40, 'b' * 40, 'c' * 40
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if 'symbolic-ref' in args:
                return CompletedProcess(args, 0, 'main', '')
            if 'merge-base' in args:
                return CompletedProcess(args, 0 if args[-1] == first_base else 1, '', '')
            raise AssertionError(args)
        proof = _published_checkout(['git', '-C', '/clone'], 'owner/app', head, {first_base}, run)
        self.assertEqual(proof['basis'], 'main_ancestor_of_verified_candidate_bases')
        self.assertEqual(proof['verified_bases'], [first_base])
        with self.assertRaises(Unverified):
            _published_checkout(['git', '-C', '/clone'], 'owner/app', head, {first_base, second_base}, run)
        self.assertFalse(any('gh' in args for args in calls))

    def test_mixed_held_and_critical_wait_does_not_pin_auditor(self):
        self.inventory.append({'number':11, 'pull_request':{}})
        def run(args, **kwargs):
            if args[:4] == ['gh','pr','view','11']:
                return CompletedProcess(args, 0, json.dumps({'number':11,'state':'OPEN','labels':[{'name':'hold'},{'name':'ready-for-audit'}]}), '')
            return self.fake_run(args, **kwargs)
        result = check(self.config, self.state, 'owner/app', run=run)
        self.assertTrue(result['eligible'], result)
        self.assertEqual([c['pr'] for c in result['evidence']['candidates']], [10])
        self.live['labels'].append({'name':'hold'})
        result = check(self.config, self.state, 'owner/app', run=run)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'], 'no_bounded_audit_queue')

    def test_slow_human_wait_read_expires_whole_proof(self):
        clock = [0]
        def slow(args, **kwargs):
            self.assertLessEqual(kwargs['timeout'], 30)
            value = self.fake_run(args, **kwargs)
            clock[0] += 31
            return value
        with patch('forge.handoff.time.monotonic', side_effect=lambda: clock[0]):
            result = check(self.config, self.state, 'owner/app', run=slow)
        self.assertEqual(result['reason'], 'verification_deadline_exceeded')
        self.assertFalse(result['eligible'])

    def test_missing_local_base_uses_exact_remote_ancestry(self):
        from forge.human_wait import _canonical_ancestor
        from forge.handoff import Unverified
        calls = []
        response = {'status':'ahead','base_commit':{'sha':self.head},'behind_by':0}
        def run(args, **kwargs):
            calls.append(args)
            if args[0] == 'git':
                return CompletedProcess(args, 128, '', 'missing local object')
            self.assertIn(f'repos/owner/app/compare/{self.head}...{self.base}', args)
            return CompletedProcess(args, 0, json.dumps(response), '')
        _canonical_ancestor(['git','-C','/clone'],'owner/app',self.head,self.base,run)
        self.assertEqual(len(calls),2)
        for field,value in [('status','diverged'),('base_commit',{'sha':self.base}),('behind_by',1)]:
            old=response[field];response[field]=value
            with self.assertRaises(Unverified):
                _canonical_ancestor(['git','-C','/clone'],'owner/app',self.head,self.base,run)
            response[field]=old
        self.assertFalse(any('fetch' in call for call in calls))
