"""Read-only repository planning on the existing disposable worker boundary."""
import json
import time
import uuid
from urllib.parse import quote

from ..store.auxiliary import active
from ..store.executions import provider_available
from ..store.models import TransitionError, VersionConflict
from ..store.repository_planning import RepositoryPlanning
from ..store.transitions import manifest_hash
from .github import GitHubUnavailable, SHA
from .runner import ContainerRunner, RunnerCapacity, RunnerError
from .store_inventory import StoreInventory


class RepositoryPlanner:
    def __init__(self,store,github,*,config,settings,observations,runner=ContainerRunner,clock=time.time):
        self.store,self.github,self.config,self.settings,self.observations=store,github,config,settings,observations
        self.runner,self.clock=runner,clock
        self.plans=RepositoryPlanning(store,clock=clock)
        self.inventory=StoreInventory(store,settings,github,config=config,clock=clock)

    def _repo(self,repo_id):
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT id,full_name AS name FROM repositories WHERE id=?',(repo_id,)).fetchone()
        if not row:raise TransitionError('Repository no longer exists')
        return dict(row)

    def refresh(self,repo):
        item={'repo_id':repo['id'],'repo':repo['name']}
        numbers={row['number'] for row in self.github.pages('controller',item,'/issues?state=open') if 'pull_request' not in row}
        with self.store.read_snapshot() as db:
            numbers.update(row[0] for row in db.execute("SELECT github_number FROM issues WHERE repository_id=? AND github_state='open'",(repo['id'],)))
        observed=set()
        for number in sorted(numbers):
            key=f"{repo['id']}:{number}"
            with self.store.read_snapshot() as db:
                member=db.execute('SELECT m.unit_id FROM unit_issues m JOIN units u ON u.id=m.unit_id WHERE m.issue_id=? AND m.left_at IS NULL '
                    "AND u.state NOT IN ('merged','closed','dissolved')",(key,)).fetchone()
            if member:
                if member[0] not in observed:
                    self.observations.step(member[0]);observed.add(member[0])
            else:
                remote,requirements=self.github.issue({**item,'number':number})
                self.inventory.record(repo,remote,requirements)
        # Planner capture may include holds and ownership. Acceptance separately
        # checks current authority; refresh never admits backlog issues.

    def request(self,repo_id,*,request_id,actor_id):
        repo=self._repo(repo_id)
        branch=self.github.target({'repo_id':repo_id,'repo':repo['name']})
        return self.plans.queue(repo_id,request_id=request_id,actor_id=actor_id,base_branch=branch)

    def _start(self,run):
        repo=self._repo(run['repository_id'])
        if not self.settings.admission(repo['name'])['allowed']:return {'waiting':'repository'}
        pool=self.config.get('provider_pool') or [dict(id=None,**{k:self.config.get(k) for k in ('provider','model','provider_environment','provider_auth_file_env')})]
        now=self.clock();ready=self.config.get('provider_ready')
        with self.store.read_snapshot() as db:
            entry=next((e for e in pool if (ready is None or e['id'] in ready) and provider_available(db,e['provider'],e['id'],{**self.config,**{k:v for k,v in e.items() if k!='id'}},now)),None)
        if entry is None:return {'waiting':'coding_account'}
        self.refresh(repo)
        self.plans.refresh_inputs(run['id'])
        context=self.plans.context(run['id'])
        with self.store.read_snapshot() as db:
            branch=db.execute('SELECT base_branch FROM triage_proposals WHERE id=?',(run['id'],)).fetchone()[0]
        item={'repo_id':repo['id'],'repo':repo['name']}
        head=(self.github.call('controller',item,'GET','/branches/'+quote(branch,safe='')).get('commit') or {}).get('sha')
        if not isinstance(head,str) or not SHA.fullmatch(head):raise GitHubUnavailable('Repository planning head could not be verified')
        for issue in context['issues']:
            if issue['candidate_id']:
                pr=self.github.call('controller',item,'GET',f"/pulls/{issue['pull_number']}")
                issue['candidate_author']={k:(pr.get('user') or {}).get(k) for k in ('id','type','login')}
        worker={**self.config,**{k:v for k,v in entry.items() if k!='id'}}
        job=dict(id=str(uuid.uuid4()),slot='repository-triage',role='triage',proposal_id=run['id'],repo=repo['name'],repo_id=repo['id'],
            candidate=None,head=head,base_branch=branch,deadline=now+1800,fenced=False,cleanup=False,created_at=now,
            provider_id=entry['id'],runner_config=worker,context_hash=manifest_hash(context))
        self.plans.reserve(run['id'],job,context)
        return self.drive(job)

    def drive(self,job):
        job=self.plans.job(job['id']);runner=self.runner(job['runner_config'])
        try:
            if job['fenced'] or self.clock()>=job['deadline']:
                if not job['fenced']:job=self.plans.progress(job['id'],fenced=True)
                if not job['cleanup']:
                    runner.reset(job);job=self.plans.progress(job['id'],cleanup=True)
                return self._finish(job)
            current=runner.inspect(job)
            if current is None:
                if job.get('container_id'):
                    self.plans.progress(job['id'],fenced=True)
                    return {'phase':'collecting'}
                identifier=runner.create(job)
                job=self.plans.progress(job['id'],container_id=identifier)
                current=runner.inspect(job)
            if current['status']=='created':
                prepared=self.plans.job(job['id'],context=True)
                runner.prepare(prepared,self.github.token('recovery',job['repo_id'],worker=True))
                if self.plans.job(job['id'])['fenced']:return {'phase':'collecting'}
                runner.start(job)
                return {'phase':'running'}
            if current['running']:return {'phase':'running'}
            capture=runner.capture(job)
            # Planning keeps its structured proposal evidence. Raw terminal output
            # is not required for acceptance and is not retained in this domain.
            capture.pop('logs',None)
            try:job=self.plans.progress(job['id'],capture=capture,fenced=True)
            except TransitionError:
                self.plans.progress(job['id'],fenced=True)
                return {'phase':'collecting','error':'triage_capture_invalid'}
            runner.reset(job);job=self.plans.progress(job['id'],cleanup=True)
            return self._finish(job)
        except RunnerCapacity:
            return {'waiting':'capacity'}
        except (RunnerError,GitHubUnavailable):
            # Owned executions are fenced before another provider can acquire the
            # slot. The next cycle retries teardown even during a GitHub outage.
            self.plans.progress(job['id'],fenced=True)
            return {'phase':'collecting','error':'triage_worker_unavailable'}

    def _finish(self,job):
        capture=job.get('capture') or {};result=capture.get('result')
        valid=(isinstance(result,dict) and result.get('attempt')==job['id'] and result.get('role')=='triage'
            and result.get('outcome')=='pass' and isinstance(result.get('rows'),list))
        error=None
        if not valid:error='triage_result_invalid'
        else:
            try:
                self.plans.publish(job['proposal_id'],expected_version=1,rows=result['rows'],rationale=result.get('summary',''),
                    request_id=job['id']+':publish',actor_id='supervisor:triage')
            except (TransitionError,ValueError,TypeError,KeyError):error='triage_proposal_invalid'
        self.plans.settle_run(job['proposal_id'],error=error)
        return {'phase':'failed' if error else 'complete','error':error}

    def cycle(self,*,after=None,limit=50,launch=True):
        owned=active(self.store,'triage')
        if owned:return {'items':[self.drive(job) for job in owned],'next_cursor':None}
        # Cleaned captures survive interruption between cleanup and publication.
        pending=self.plans.runs(active=True)
        if pending:return {'items':[self._finish(self.plans.job(row['execution_id'])) for row in pending],'next_cursor':None}
        if not launch or not self.config.get('enabled') or self.settings.paused():return {'items':[],'next_cursor':None}
        queued=self.plans.runs()
        if not queued:
            with self.store.read_snapshot() as db:
                due_rows=db.execute('SELECT r.id AS repository_id,r.full_name FROM repositories r LEFT JOIN repository_planning p ON p.repository_id=r.id WHERE (p.proposals_enabled=1 OR p.repository_id IS NULL) AND coalesce(p.next_triage_at,0)<=? ORDER BY coalesce(p.next_triage_at,0),r.id LIMIT 50',(self.clock(),)).fetchall()
            due=next((row for row in due_rows if self.settings.admission(row['full_name'])['allowed']),None)
            if due:
                self.request(due[0],request_id=str(uuid.uuid4()),actor_id='supervisor:nightly-triage')
                queued=self.plans.runs()
        if not queued:return {'items':[],'next_cursor':None}
        results=[]
        for run in queued:
            try:result=self._start(run)
            except GitHubUnavailable:
                results.append({'waiting':'github','proposal_id':run['id']});continue
            except (TransitionError,ValueError):
                self.plans.settle_run(run['id'],error='triage_inputs_changed')
                results.append({'error':'triage_inputs_changed','proposal_id':run['id']});continue
            results.append(result)
            if result.get('waiting')!='repository':break
        return {'items':results,'next_cursor':None}

    def cleanup(self):
        for job in active(self.store,'triage'):
            self.plans.progress(job['id'],fenced=True)
            self.drive(job)
        return len(active(self.store,'triage'))
