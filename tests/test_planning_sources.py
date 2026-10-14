import json
import re
import shutil
import subprocess
import unittest

from forge.planning_sources import PlanningSources
from forge.factory.harness import schema as runner_schema
from forge.planning_contract import provider_triage_rows
from forge.store.repository_planning import RepositoryPlanning


class PlanningSourceTests(unittest.TestCase):
    def setUp(self):
        self.head='a'*40
        self.prefix='https://github.com/XRPL-Commons/serious-game/'
        self.authority=PlanningSources('XRPL-Commons/serious-game',self.head,[
            {'requirements_revision_id':'revision-a','github_number':749,'candidate_id':None,'pull_number':None},
            {'requirements_revision_id':'revision-b','github_number':752,'candidate_id':'candidate-b','pull_number':800}])

    def source(self,kind,identity,path):
        return {'kind':kind,'identity':identity,'url':self.prefix+path,'description':'Captured evidence'}

    def test_exact_captured_identity_url_pairs_remain_host_authority(self):
        valid=[self.source('issue_revision','revision-a','issues/749'),
               self.source('candidate','candidate-b','pull/800'),
               self.source('commit',self.head,'commit/'+self.head),
               self.source('document',self.head,'blob/'+self.head+'/README.md')]
        for source in valid:
            with self.subTest(source=source):self.assertTrue(self.authority.matches(source))
        invalid=[self.source('issue','revision-a','issues/749'),
                 self.source('issue_revision','revision-a','issues/752'),
                 self.source('candidate','candidate-b','pull/749'),
                 self.source('commit','b'*40,'commit/'+'b'*40),
                 self.source('document',self.head,'blob/main/README.md'),
                 self.source('decision',self.head,'commit/'+self.head)]
        for source in invalid:
            with self.subTest(source=source):self.assertFalse(self.authority.matches(source))

    def test_generated_branches_exclude_invalid_kinds_and_pin_head_and_url(self):
        schema=self.authority.schema()
        branches={row['properties']['kind']['enum'][0]:row['properties'] for row in schema['anyOf']}
        self.assertEqual(set(branches),{'issue_revision','candidate','commit','document'})
        for kind in ('commit','document'):
            self.assertEqual(branches[kind]['identity']['enum'],[self.head])
        self.assertEqual(branches['commit']['url']['enum'],[self.prefix+'commit/'+self.head])
        for url,expected in [(self.prefix+'blob/'+self.head+'/README.md',True),
                             (self.prefix+'blob/main/README.md',False),
                             ('https://github.com/example/other/blob/'+self.head+'/README.md',False)]:
            self.assertEqual(bool(re.search(branches['document']['url']['pattern'],url)),expected)
        self.assertIn('requirements_revision_id',branches['issue_revision']['identity']['description'])
        self.assertIn('candidate_id',branches['candidate']['identity']['description'])

    def test_empty_backlog_has_only_assigned_commit_sources_without_growing_schema(self):
        empty=PlanningSources('XRPL-Commons/serious-game',self.head,[]).schema()
        self.assertEqual([v['properties']['kind']['enum'][0] for v in empty['anyOf']],['commit','document'])
        many=PlanningSources('XRPL-Commons/serious-game',self.head,[
            {'requirements_revision_id':str(i),'github_number':i+1,'candidate_id':'c'+str(i),'pull_number':i+1000}
            for i in range(2000)])
        self.assertEqual(many.schema(),self.authority.schema())

    def test_close_evidence_retains_canonical_broader_reference_contract(self):
        close=next(b for b in provider_triage_rows(rules='')['anyOf'] if b['properties']['action']['enum']==['close'])
        kinds=close['properties']['parameters']['properties']['evidence']['items']['properties']['kind']['enum']
        self.assertEqual(set(kinds),{'issue_revision','candidate','commit','decision','document'})
        self.assertNotIn('issue',kinds)
        row={'action':'close','members':['1:1'],'reason':'Recorded completion','rule_quote':None,
             'parameters':{'evidence':[self.source('commit','b'*40,'commit/'+'b'*40)]}}
        self.assertEqual(RepositoryPlanning._row(row,'')['action'],'close')

    @unittest.skipUnless(shutil.which('node'), 'ECMAScript schema checks require Node.js')
    def test_full_runner_schema_patterns_compile_as_unicode_ecmascript(self):
        contract=runner_schema({'id':'triage-test','role':'triage','context':{
            'rows_schema':provider_triage_rows(rules='Keep useful work.'),
            'note_sources_schema':self.authority.schema()}})
        script = """
const schema = JSON.parse(require('fs').readFileSync(0, 'utf8'));
let patterns = 0;
function walk(value) {
  if (!value || typeof value !== 'object') return;
  if (typeof value.pattern === 'string') { new RegExp(value.pattern, 'u'); patterns++; }
  for (const child of Object.values(value)) walk(child);
}
walk(schema);
process.stdout.write(JSON.stringify({patterns}));
"""
        result=subprocess.run(['node','-e',script],input=json.dumps(contract),text=True,capture_output=True,timeout=10,check=True)
        self.assertGreater(json.loads(result.stdout)['patterns'], 3)

    @unittest.skipUnless(shutil.which('node'), 'ECMAScript schema checks require Node.js')
    def test_url_patterns_preserve_literal_repository_head_and_source_paths(self):
        cases=[]
        for repository in ('XRPL-Commons/serious-game', 'XRPL-Commons/repo.with-dots_and_underscores'):
            authority=PlanningSources(repository,self.head,list(self.authority.issues.values()))
            prefix='https://github.com/'+repository+'/'
            branches={row['properties']['kind']['enum'][0]:row['properties'] for row in authority.schema()['anyOf']}
            for kind,identity,path in (
                ('issue_revision','revision-a','issues/749'),
                ('candidate','candidate-b','pull/800'),
                ('document',self.head,'blob/'+self.head+'/docs/getting-started.md')):
                valid=prefix+path
                urls=[(valid,True),('https://github.com/elsewhere/repo/'+path,False),
                      (valid.replace('github.com','githubXcom'),False),
                      (valid.replace(repository,repository.replace('-','X').replace('.','X')),False),
                      ('http'+valid[5:],False),
                      (prefix+path.replace('blob/'+self.head, 'blob/main'),kind!='document')]
                if kind!='document':
                    urls.extend([(valid+'/extra',False),(prefix+('issues/' if kind=='issue_revision' else 'pull/')+'0',False)])
                for url,expected in urls:
                    source={'kind':kind,'identity':identity,'url':url,'description':'Captured evidence'}
                    with self.subTest(repository=repository,kind=kind,url=url):
                        self.assertEqual(authority.matches(source),expected)
                    cases.append({'pattern':branches[kind]['url']['pattern'],'url':url,'expected':expected})
            # Schema deliberately bounds syntax independently of backlog size;
            # the host still rejects a well-formed URL paired with the wrong pin.
            self.assertFalse(authority.matches({'kind':'issue_revision','identity':'revision-a','url':prefix+'issues/752'}))
            self.assertFalse(authority.matches({'kind':'candidate','identity':'candidate-b','url':prefix+'pull/749'}))
        script = """
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(cases.map(row => new RegExp(row.pattern, 'u').test(row.url))));
"""
        result=subprocess.run(['node','-e',script],input=json.dumps(cases),text=True,capture_output=True,timeout=10,check=True)
        self.assertEqual(json.loads(result.stdout),[row['expected'] for row in cases])
