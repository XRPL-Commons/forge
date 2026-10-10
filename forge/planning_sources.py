"""Captured planner sources: one identity/URL authority for notes and handoffs."""
from copy import deepcopy
import re

from .planning_contract import action_schemas, provider_schema


class PlanningSources:
    def __init__(self, repository, head, issues):
        self.prefix='https://github.com/'+repository+'/'
        self.head=head
        self.issues={item['requirements_revision_id']:item for item in issues}
        self.candidates={item['candidate_id']:item for item in issues if item['candidate_id']}

    def matches(self, source):
        kind,identity,url=source['kind'],source['identity'],source['url']
        if kind=='issue_revision':
            item=self.issues.get(identity)
            return item is not None and url==self.prefix+'issues/'+str(item['github_number'])
        if kind=='candidate':
            item=self.candidates.get(identity)
            return item is not None and url==self.prefix+'pull/'+str(item['pull_number'])
        if kind=='commit':
            return identity==self.head and url==self.prefix+'commit/'+self.head
        if kind=='document':
            return identity==self.head and url.startswith(self.prefix+'blob/'+self.head+'/')
        return False

    def schema(self):
        # Keep this bounded independently of backlog size. The host checks each
        # captured issue/candidate identity and its paired URL, never just syntax.
        source=provider_schema(action_schemas()['note']['properties']['sources']['items'])
        branches=[]
        for kind, captured, path, identity_field in (
            ('issue_revision',self.issues,'issues/','requirements_revision_id'),
            ('candidate',self.candidates,'pull/','candidate_id'),
            ('commit',True,'commit/',None),('document',True,'blob/',None)):
            if not captured:continue
            branch=deepcopy(source);props=branch['properties']
            props['kind']={'type':'string','enum':[kind]}
            if identity_field:
                props['identity']['description']='Copy the exact '+identity_field+' from one context.issues entry; pair its GitHub URL.'
                props['url']['pattern']='^'+re.escape(self.prefix+path)+r'[1-9][0-9]*$'
            else:
                props['identity']={'type':'string','enum':[self.head]}
                if kind=='commit':props['url']={'type':'string','enum':[self.prefix+path+self.head]}
                else:props['url']['pattern']='^'+re.escape(self.prefix+path+self.head+'/')
            branches.append(branch)
        return {'anyOf':branches}
