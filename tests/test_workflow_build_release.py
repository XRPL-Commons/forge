import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from forge.control import atomic_json,snapshot_path
from forge.workflow_build_release import check


class Refused(ValueError):pass

class TrackedBuildReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.state=self.root/'state';self.config=self.root/'config';self.config.mkdir()
        self.clone=self.root/'clone'
        (self.config/'app.repo').write_text(f'REPO=Owner/App\nOWNER_LABEL=auto:owned-by-worker\nRECOVERY_AUTHOR=worker\nCLONE_BUILD={self.clone}\n')
        self.identity={'session_id':'$1','session_created':50,'pane_id':'%1','pane_pid':10,'processes':[10,11]}
        self.a={'id':'a1','repo':'owner/app','role':'build','session':'App-build','state':'submitted',
                'submitted_at':60,'generation':'$1|50|%1|10','issue_numbers':[1],'pr':None}
        self.slot={'session':'App-build','assignment_id':'a1','generation':self.a['generation']}
        self.view={'slots':[self.slot],'assignments':[self.a]}
        self.snapshot={'repo':'Owner/App','status':'ok','enabled':True,'observed_at':99,
            'counts':{'builder':3},'work':{'ready':[3],'sentback':[4],'conflicts':[5],'limbo':[]},'schedule':{'active':[1,2]}}
        atomic_json(snapshot_path(self.state,'Owner/App'),self.snapshot)
        self.head='a'*40;self.merge='b'*40;self.main='c'*40;self.tree='d'*40;self.status=''
        self.pr={'number':10,'url':'https://github.com/Owner/App/pull/10','state':'OPEN','isDraft':False,
            'headRefOid':self.head,'baseRefOid':self.main,'author':{'login':'worker'},
            'labels':[{'name':'ready-for-review'}],
            'closingIssuesReferences':[{'number':1,'url':'https://github.com/Owner/App/issues/1'}]}
        self.candidates=[{'number':10,'base':{'repo':{'full_name':'Owner/App'}}}]
        self.calls=[];self.compare='ahead'

    def run_command(self,args,**kwargs):
        self.calls.append(args)
        if args[0]=='git':
            cmd=args[3:]
            if cmd==['rev-parse','--show-toplevel']:return str(self.clone)
            if cmd==['remote','get-url','origin']:return 'git@github.com:Owner/App.git'
            if cmd[0]=='status':return self.status
            if cmd==['rev-parse','HEAD']:return self.head
            if cmd==['rev-parse',self.head+'^{tree}']:return self.tree
        elif args[:3]==['gh','pr','view']:return json.dumps(self.pr)
        elif args[:2]==['gh','api']:
            endpoint=args[2]
            if '/pulls?' in endpoint:return json.dumps(self.candidates)
            if '/git/commits/' in endpoint:return json.dumps({'sha':self.merge,'tree':{'sha':self.tree}})
            if '/git/ref/heads/main' in endpoint:return json.dumps({'object':{'sha':self.main}})
            if '/compare/' in endpoint:return json.dumps({'status':self.compare,'base_commit':{'sha':self.merge},'behind_by':0})
        self.fail(args)

    def decision(self,view=None,identity=None):
        with patch('forge.workflow_build_release.read_state',side_effect=view or (lambda _:copy.deepcopy(self.view))):
            return check(self.config,self.state,'App-build',None,100,unverified=Refused,
                read_command=lambda args,run:self.run_command(args),identify_session=lambda s,r:copy.deepcopy(identity or self.identity))

    def test_own_published_handoff_ignores_unrelated_ready_returned_and_conflicting_work(self):
        result=self.decision()
        self.assertTrue(result['eligible']);self.assertEqual(result['evidence']['assignment_id'],'a1')
        self.assertEqual(result['evidence']['issues'],[1]);self.assertEqual(result['evidence']['pr'],10)
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_own_returned_draft_or_held_pr_cannot_release(self):
        for labels in [['changes-requested'],['hold','ready-for-review'],['ready-for-review','ready-for-audit'],[]]:
            self.pr['labels']=[{'name':n} for n in labels]
            with self.subTest(labels=labels),self.assertRaises(Refused):self.decision()
        self.pr['labels']=[{'name':'ready-for-review'}];self.pr['isDraft']=True
        with self.assertRaises(Refused):self.decision()

    def test_dirty_unpublished_and_changed_generation_are_preserved(self):
        self.status='?? unfinished'
        with self.assertRaisesRegex(Refused,'worktree_not_clean'):self.decision()
        self.status='';self.pr['headRefOid']='f'*40
        with self.assertRaises(Refused):self.decision()
        self.pr['headRefOid']=self.head
        with self.assertRaisesRegex(Refused,'generation_changed'):self.decision(identity={**self.identity,'session_id':'$2'})
        self.candidates=[]
        with self.assertRaisesRegex(Refused,'published_pr_missing'):self.decision()

    def test_canonical_links_must_cover_exact_assignment_and_agree(self):
        for refs in [[{'number':2,'url':'https://github.com/Owner/App/issues/2'}],
            [{'number':1,'url':'https://github.com/Owner/App/issues/1','repository':{'nameWithOwner':'Other/App'}}],
            [{'number':1,'url':'https://github.com/Other/App/issues/1'}],[]]:
            self.pr['closingIssuesReferences']=refs
            with self.subTest(refs=refs),self.assertRaises(Refused):self.decision()

    def test_merged_assignment_requires_published_main_ancestry(self):
        self.pr.update(state='MERGED',mergedAt='2026-09-08',mergeCommit={'oid':self.merge})
        result=self.decision();self.assertEqual(result['evidence']['checkout_basis'],'merged_pr_on_published_main')
        self.compare='diverged'
        with self.assertRaises(Refused):self.decision()

    def test_absent_assignment_uses_legacy_path_but_tracked_corruption_does_not(self):
        self.view['slots']=[];self.assertIsNone(self.decision())
        self.view['slots']=[self.slot];self.view['assignments']=[]
        with self.assertRaisesRegex(Refused,'assignment_missing'):self.decision()

    def test_assignment_changes_during_proof_block_release(self):
        values=[copy.deepcopy(self.view),copy.deepcopy(self.view)]
        values[1]['assignments'][0]['generation']='$2|55|%3|20'
        with self.assertRaisesRegex(Refused,'assignment_changed'):self.decision(view=values)

    def test_returned_pr_assignment_stays_on_same_pr(self):
        self.a['pr']={'number':10,'head':'e'*40,'base':self.main}
        self.assertTrue(self.decision()['eligible'])
        self.assertFalse(any('/pulls?' in ' '.join(c) for c in self.calls))
        self.pr['number']=11
        with self.assertRaises(Refused):self.decision()

    def test_multiple_publications_for_same_assignment_remain_ambiguous(self):
        self.candidates.append({'number':11,'base':{'repo':{'full_name':'Owner/App'}}})
        original=self.run_command
        def command(args,**kwargs):
            if args[:3]==['gh','pr','view']:
                return json.dumps({**self.pr,'number':int(args[3]),'url':f'https://github.com/Owner/App/pull/{args[3]}'})
            return original(args,**kwargs)
        self.run_command=command
        with self.assertRaisesRegex(Refused,'publication_not_unique'):self.decision()

    def test_actual_merged_commit_checkout_is_published_without_local_tree_guess(self):
        self.head=self.merge
        self.pr.update(state='MERGED',mergedAt='2026-09-08',mergeCommit={'oid':self.merge})
        self.assertTrue(self.decision()['eligible'])
        self.assertFalse(any('/git/commits/' in ' '.join(c) for c in self.calls))
        self.compare='diverged'
        with self.assertRaises(Refused):self.decision()
