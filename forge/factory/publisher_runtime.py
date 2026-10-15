"""Connect immutable source preparation, retained artifacts and publication."""
import time
import math
import re
import uuid

from ..app_access import AccessError
from ..request_metrics import deadline as request_deadline, RequestBudgetExhausted
from ..store.models import Command
from .github import GitHubUnavailable, GitHubBudgetExhausted
from ..publication_artifacts import PublicationClaim, inspect_output
from ..mirror.policy import MirrorError
from ..store.executions import ExecutionQueries
from . import source_inputs
from .publisher import Publisher


def publication_authorization(job,github):
    """Freeze only the target already authorized by the reserved assignment."""
    from .delegation_contracts import task_assignment
    if job['role']!='build' or job.get('builder_stage')=='planning' or task_assignment(job):return {}
    manifest=job['input_manifest'];target=manifest.get('publication_target') or {}
    previous=manifest.get('previous_publication') or {}
    candidate=job.get('candidate') or {}
    source={}
    if target.get('kind')=='existing':
        source=next((row for row in manifest.get('existing_candidates',[]) if row['candidate_id']==target.get('candidate_id')),None)
        if source is None:raise PublicationClaim('publication_target_missing')
    expected=job.get('resume_head') or previous.get('head_oid') or source.get('head_oid') or candidate.get('head')
    number=job.get('resume_pr') or previous.get('pull_number') or source.get('pull_number') or candidate.get('pr')
    branch=job['branch']
    if expected is None and number is None:
        return dict(kind='forge_owned',branch=branch,expected_head_oid=None,pull_number=None,
                    author=github.identity('controller',job['repo_id']),replace_ref=False)
    if not expected or not number:raise PublicationClaim('publication_target_incomplete')
    pinned_author=job.get('publication_author') or previous.get('author') or source.get('author') or target.get('author')
    pr=github.pr(job,number);author={key:(pr.get('user') or {}).get(key) for key in ('id','login','type')}
    expected_branch=job.get('resume_branch') or previous.get('branch') or source.get('branch') or candidate.get('branch') or branch
    if (branch!=expected_branch or pr.get('number')!=number or pr.get('state')!='open' or pr.get('merged')
            or type(pr.get('draft')) is not bool or (pr.get('head') or {}).get('sha')!=expected
            or (pr.get('head') or {}).get('ref')!=branch or ((pr.get('head') or {}).get('repo') or {}).get('id')!=job['repo_id']
            or (pr.get('base') or {}).get('ref')!=job['target_branch'] or ((pr.get('base') or {}).get('repo') or {}).get('id')!=job['repo_id']
            or (pr.get('base') or {}).get('sha')!=manifest['base_oid'] or pinned_author is not None and pinned_author!=author):
        raise PublicationClaim('publication_target_changed')
    transfer=target.get('kind')=='existing' and target.get('ownership_transfer') is True and target.get('author')==author
    from .github import bot_logins
    # A PR the retired build or controller App opened stays Forge's after the move to one App.
    forge_owned=author['type']=='Bot' and author['login'] in {*bot_logins(github,'build'),*bot_logins(github,'controller')}
    if not (transfer or forge_owned):raise PublicationClaim('publication_target_not_owned')
    # The legacy Build App may own this draft while the Controller publishes.
    # Do not assume the Controller can mark another App's draft ready. Retain
    # the exact target and ask for that explicit transition before launch.
    if pr['draft']:raise PublicationClaim('publication_draft_requires_ready')
    return dict(kind='ownership_transfer' if transfer else 'forge_owned',branch=branch,expected_head_oid=expected,
                pull_number=number,author=author,replace_ref=True)


def publication_access(job,github):
    """Read current grants on the same App installation without changing them."""
    if time.monotonic()>=min(getattr(github,'deadline',float('inf')),request_deadline()):
        raise GitHubBudgetExhausted('Publication access observation is due next cycle')
    connection=github.connection('controller',job['repo_id'])
    try:
        installation=github.client.request('GET','/app/installations/'+str(connection['installation_id']),github.client.jwt(connection))
    except AccessError as error:raise GitHubUnavailable('Publication App access is unavailable',status=error.status) from None
    except RequestBudgetExhausted:raise GitHubBudgetExhausted('Publication access observation is due next cycle') from None
    if installation.get('id')!=connection['installation_id'] or installation.get('app_id')!=connection['app_id']:
        raise PublicationClaim('publication_app_changed')
    repository=github.call('controller',job,'GET','')
    if repository.get('id')!=job['repo_id']:raise PublicationClaim('publication_repository_changed')
    return dict(app_id=connection['app_id'],installation_id=connection['installation_id'],repository_id=job['repo_id'],
                permissions=installation.get('permissions') or {})


class SigningCapabilities:
    """A completed host probe proves an API strategy for one current target context."""
    def __init__(self, store, github):
        from ..store.configuration import Configuration
        self.configuration, self.github = Configuration(store), github

    def __call__(self, job):
        from ..publication_qualification import signing_context, QualificationError
        connection = self.github.connection('controller', job['repo_id'])
        pending = dict(app_id=connection['app_id'], strategy='pending', qualified=False)
        record = self.configuration.get('publication_signing', str(job['repo_id']))
        if record is None:
            return pending
        proof = record['settings']
        fields = {'version', 'repository_id', 'app_id', 'installation_id', 'strategy',
                  'probe_repository_id', 'commit_oid', 'tree_oid', 'verified', 'exact_ref', 'signing_rule_accepted',
                  'observed_at', 'target_branch', 'target_rules', 'probe_ref', 'probe_receipt_id', 'signing_requirement'}
        if (not isinstance(proof, dict) or set(proof) != fields or type(proof['version']) is not int or proof['version'] != 2
                or any(type(proof[k]) is not int or proof[k] <= 0 for k in ('repository_id', 'app_id', 'installation_id', 'probe_repository_id'))
                or proof['repository_id'] != job['repo_id'] or proof['app_id'] != connection['app_id']
                or proof['installation_id'] != connection['installation_id'] or proof['target_branch'] != job.get('target_branch')
                or proof['strategy'] not in ('rest', 'graphql') or not isinstance(proof['target_rules'], list)
                or any(not isinstance(proof[k], str) or not re.fullmatch('[0-9a-f]{40}', proof[k]) for k in ('commit_oid', 'tree_oid'))
                or proof['verified'] is not True or proof['exact_ref'] is not True
                or proof['signing_requirement'] != ('accepted' if proof['target_rules'] else 'not_required')
                or proof['signing_rule_accepted'] is not bool(proof['target_rules'])
                or not isinstance(proof['probe_receipt_id'], str) or not re.fullmatch('[a-f0-9]{32}', proof['probe_receipt_id'])
                or type(proof['observed_at']) not in (int, float) or not math.isfinite(proof['observed_at']) or proof['observed_at'] <= 0):
            return pending
        receipt = self.configuration.get('publication_signing_probe', proof['probe_receipt_id'])
        if receipt is None or receipt['settings'].get('stage') != 'qualified':
            return pending
        if any(receipt['settings'].get(key) != proof[key] for key in ('repository_id', 'app_id', 'installation_id',
                'probe_repository_id', 'target_branch', 'target_rules', 'probe_ref', 'strategy', 'commit_oid', 'tree_oid')):
            return pending
        try:
            current = signing_context(lambda method, suffix: self.github.call('controller', job, method, suffix), proof['target_branch'])
        except QualificationError:
            return pending
        if current != proof['target_rules']:
            return pending
        return dict(app_id=proof['app_id'], strategy=proof['strategy'], qualified=True,
                    qualification_revision=record['revision'])


class PublicationRuntime:
    def __init__(self,store,github,source_preparation,*,signing_capability=None,**options):
        self.store,self.github,self.sources=store,github,source_preparation
        self.jobs=ExecutionQueries(store);self.cached={}
        self.capability=signing_capability or (lambda job:dict(app_id=github.connection('controller',job['repo_id'])['app_id'],
                                                             strategy='pending',qualified=False))
        self.capabilities={}
        self.publisher=Publisher(store,github,self.output,signing_capability=self.qualified_capability,**options)

    def qualified_capability(self,job):
        if job['id'] not in self.capabilities:
            capability=self.capability(job)
            if capability.get('qualified'):
                access=publication_access(job,self.github)
                if access['app_id']!=capability['app_id']:raise PublicationClaim('publication_app_changed')
                capability={**capability,'workflows_granted':access['permissions'].get('workflows')=='write'}
            self.capabilities[job['id']]=capability
        return self.capabilities[job['id']]

    def output(self,job_id):
        if job_id in self.cached:return self.cached[job_id]
        job=self.jobs.job(job_id);source=job.get('source_input')
        if source is None:raise PublicationClaim('publication_source_missing')
        source=source_inputs.validate(source)
        repository=self.sources.access.resolve(job['repo'])
        if repository.repository_id!=source['repository_id'] or repository.canonical_name!=source['canonical_repository']:
            raise PublicationClaim('publication_repository_changed')
        capture=self.jobs.capture(job_id);head=(capture.get('result') or {}).get('output_commit')
        with self.sources.git_store.base_objects(repository,[source['base_oid'],*source['input_oids']],source_inputs.digest(source)) as objects:
            verified=inspect_output(self.jobs.bundle(job_id),head,source,objects)
        self.cached[job_id]=verified
        return verified

    def ensure(self,job,report):
        try:
            with self.store.read_snapshot() as db:
                row=db.execute('SELECT id FROM publication_intents WHERE job_id=?',(job['id'],)).fetchone()
            if row:return self.publisher.step(row[0])
            capability=self.qualified_capability(job)
            if not capability.get('qualified') or capability.get('strategy') not in ('rest','graphql'):
                return {'status':'blocked','claim':{'code':'publication_signing_unqualified'}}
            authorization=job.get('publication_authorization') or None
            if authorization is None:raise PublicationClaim('publication_authorization_missing')
            target=dict(repository_id=job['repo_id'],repository=job['repo'],branch=job['branch'],base_branch=job['target_branch'],
                expected_head_oid=authorization['expected_head_oid'],pull_number=authorization['pull_number'],authorization=authorization,
                app_id=capability['app_id'],strategy=capability['strategy'],signing_qualified=True)
            return self.publisher.ensure(job,self.output(job['id']),target,report)
        except PublicationClaim as error:return {'status':'blocked','claim':error.payload()}
        except MirrorError as error:return {'status':'pending' if error.transient else 'blocked','claim':{'code':'publication_source_unavailable','reason':error.reason}}
        finally:self.cached.clear();self.capabilities.clear()

    def recheck(self,job_id):
        """Explicit repair keeps the captured output and never repeats unknown writes."""
        try:
            job=self.jobs.job(job_id)
            with self.store.read_snapshot() as db:
                row=db.execute('SELECT id FROM publication_intents WHERE job_id=?',(job_id,)).fetchone()
            if row is None:return {'status':'pending','reason':'publication_not_queued'}
            intent=self.publisher.commands.get(row[0]);claim=intent['claim'] or {}
            if not (claim.get('code')=='publication_refused' and claim.get('http_status') in (401,403,404)
                    or claim.get('code') in ('publication_signing_unqualified','publication_capability_changed','publication_app_changed')):
                return {'status':'blocked','claim':claim,'intent_id':intent['id']}
            # Reissue this repository's cached token only after a deliberate
            # repair request. Personal credentials are never involved.
            for key in list(getattr(self.github,'tokens',{})):
                if key[:2]==('controller',job['repo_id']):self.github.tokens.pop(key)
            access=publication_access(job,self.github);capability=self.qualified_capability(job);output=self.output(job_id)
            proof={**access,'source':'host_app_installation','strategy':capability['strategy'],
                'signing_qualified':capability['qualified'],'source_input_digest':source_inputs.digest(output.source),
                'output_digest':output.digest(),'observed_at':self.publisher.clock()}
            current=self.jobs.job(job_id)
            command=Command(str(uuid.uuid4()),'supervisor:publisher',job['unit_id'],current['unit_version'])
            self.publisher.commands.recheck(command,intent['id'],evidence=proof)
            return self.publisher.step(intent['id'])
        except PublicationClaim as error:return {'status':'blocked','claim':error.payload()}
        finally:self.cached.clear();self.capabilities.clear()
