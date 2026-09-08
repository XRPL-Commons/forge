from tests.host_isolation import deny_host_tmux
import copy
import json
import unittest
from types import SimpleNamespace

from forge.handoff import check, release
from tests import test_handoff as fixtures


class MultipleClaimHandoffTests(unittest.TestCase):
    save = fixtures.HandoffTests.save

    def setUp(self):
        deny_host_tmux(self)
        fixtures.HandoffTests.setUp(self)
        self.snapshot['schedule']['active'] = [1,3]
        self.snapshot['work'] = {'review':[2], 'audit':[4], 'wip':[1,3]}
        self.save()
        first = copy.deepcopy(self.pr)
        second = copy.deepcopy(self.pr)
        second.update(headRefOid='b'*40, labels=[{'name':'ready-for-audit'}],
                      closingIssuesReferences=[{'number':3,'url':'https://github.com/o/one/issues/3'}])
        self.prs = {2:first,4:second}
        self.head = second['headRefOid']
        self.pr_callback = None

    def fake_run(self,args,**kwargs):
        if args[:3] == ['gh','pr','view']:
            self.calls.append(args)
            self.pr_reads += 1
            if self.pr_callback:
                self.pr_callback(self.pr_reads)
            return SimpleNamespace(returncode=0,stdout=json.dumps(self.prs[int(args[3])]),stderr='')
        return fixtures.HandoffTests.fake_run(self,args,**kwargs)

    def decision(self):
        return check(self.config,self.state,'one-build',run=self.fake_run,now=100)

    def no_kill(self):
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_rejections_use_the_calling_cli_exception_type(self):
        from forge.builder_handoff import check_multiple
        class CallerUnverified(Exception):
            pass
        self.snapshot['schedule']['active']=[1,1]
        with self.assertRaises(CallerUnverified):
            check_multiple(None,self.snapshot,None,None,None,None,100,0,
                           read_command=None,identify_session=None,unverified=CallerUnverified)

    def test_every_claim_is_recorded_and_read_only(self):
        result=self.decision()
        self.assertTrue(result['eligible'],result)
        self.assertEqual(result['evidence']['claims'],[{'issue':1,'pr':2},{'issue':3,'pr':4}])
        self.assertEqual(result['evidence']['checkout_basis'],'pr_head')
        self.no_kill()

    def test_uncovered_claim_never_enters_completed_fallback(self):
        self.prs[4]['closingIssuesReferences']=[]
        result=release(self.config,self.state,'one-build',run=self.fake_run,now=100)
        self.assertFalse(result['released'])
        self.assertEqual(result['reason'],'multi_claim_handoff_incomplete_or_ambiguous')
        self.no_kill()

    def test_duplicate_canonical_coverage_is_ambiguous(self):
        self.prs[4]['closingIssuesReferences'].append({'number':1,'repository':{'nameWithOwner':'o/one'}})
        self.assertFalse(self.decision()['eligible']);self.no_kill()

    def test_one_pr_may_explicitly_close_both_claims(self):
        self.snapshot['work']['review']=[];self.save()
        self.prs[4]['closingIssuesReferences'].append({'number':1,'repository':{'nameWithOwner':'o/one'}})
        self.assertTrue(self.decision()['eligible'])

    def test_every_published_candidate_must_remain_owned_and_eligible(self):
        for key,value in [('isDraft',True),('state','CLOSED'),('author',{'login':'other'}),
                          ('labels',[{'name':'ready-for-review'},{'name':'hold'}]),('headRefOid','invalid')]:
            with self.subTest(key=key):
                prior=self.prs[2][key];self.prs[2][key]=value
                self.assertFalse(self.decision()['eligible']);self.prs[2][key]=prior
        self.no_kill()

    def test_cross_repo_or_conflicting_canonical_identity_is_not_coverage(self):
        for ref in [{'number':3,'repository':{'nameWithOwner':'other/one'}},
                    {'number':3,'repository':{'nameWithOwner':'other/one'},'url':'https://github.com/o/one/issues/3'},
                    {'number':3}, {'number':True,'repository':{'nameWithOwner':'o/one'}}]:
            self.prs[4]['closingIssuesReferences']=[ref]
            self.assertFalse(self.decision()['eligible'])

    def test_unpublished_checkout_dirty_work_or_children_prevent_release(self):
        for attr,value in [('head','e'*40),('status','?? leftover.txt'),('processes',self.processes+'\n12 11 node')]:
            old=getattr(self,attr);setattr(self,attr,value)
            self.assertFalse(self.decision()['eligible']);setattr(self,attr,old)
        self.no_kill()

    def test_claim_inventory_is_bounded_unique_and_matches_wip(self):
        for claims in [[1,1],[True,3],[1,3,5],list(range(1,12))]:
            self.snapshot['schedule']['active']=claims;self.save()
            self.assertFalse(self.decision()['eligible'])
        self.no_kill()

    def test_release_repeats_all_claim_proofs_and_uses_exact_generation(self):
        result=release(self.config,self.state,'one-build',run=self.fake_run,now=100)
        self.assertTrue(result['released'],result)
        self.assertEqual(self.pr_reads,4)
        self.assertIn(['tmux','kill-session','-t','$1'],self.calls)

    def test_changed_secondary_candidate_or_generation_prevents_release(self):
        for change in [lambda:self.prs[2].update(headRefOid='e'*40),
                       lambda:setattr(self,'identity','one-build|$2|90|%2|10|claude')]:
            self.pr_reads=0;self.calls=[]
            self.pr_callback=lambda n:change() if n==3 else None
            self.assertFalse(release(self.config,self.state,'one-build',run=self.fake_run,now=100)['released'])
            self.no_kill()


if __name__=='__main__':unittest.main()
