"""Disposable SQL authorities for native role qualification, never live work.

Candidate and failure records below are explicitly synthetic. They let the real
reservation, immutable-input and execution paths run without publishing a PR.
"""
from dataclasses import asdict
import json
import time
import uuid

from .store.db import Store
from .store.executions import ExecutionQueries, Launch
from .store.models import Command, Candidate
from .store.recovery import Recovery
from .store.transitions import project_unit, manifest_hash


class Fixtures:
    def __init__(self, root, repository, issue, requirements, base, config, seconds):
        self.store = Store.create(root)
        self.life = Recovery(self.store)
        self.repository, self.issue, self.base = repository, issue, base
        self.config, self.seconds = config, seconds
        self.unit = 'qualification'
        self.issue_key = f'{repository.repository_id}:{issue["number"]}'
        self.admitted = self.life.admit_issue(request_id='fixture-admit', actor_id='qualification',
            unit_id=self.unit, issue_id=self.issue_key, repository_id=repository.repository_id,
            repository_name=repository.canonical_name, issue_number=issue['number'],
            title=issue['title'], body=issue.get('body') or '', requirements=requirements,
            observed_at=time.time(), base_branch=repository.default_branch)
        self.manifest = dict(requirements_revision_id=self.admitted['revision_id'],
                             base_oid=base, policy_hash='qualification-fixture')

    def command(self):
        with self.store.read_snapshot() as db:
            version = project_unit(db, self.unit)['version']
        return Command(str(uuid.uuid4()), 'qualification', self.unit, version)

    def launch(self, *, context=None):
        return Launch('forge/qualification', 'forge-qualification', self.config['provider'],
                      time.time()+self.seconds, self.config, {}, provider_id=self.config.get('provider_id'),
                      model=self.config.get('model'),context=context or {})

    def reserve(self, role):
        if role in {'triage', 'strategist', 'discussion'}:
            return self.auxiliary(role)
        if role in {'recovery','steward'}:
            failed = self.reserve('build')
            self.clean_pipeline(failed, {'result': None, 'logs':
                'QUALIFICATION FIXTURE: an assertion expected 2 + 2 == 5; no real issue failure.', 'missing': []})
            self.life.record_failure(self.command(), failed['id'], execution_fence=failed['execution_fence'],
                input_manifest=failed['input_manifest'], evidence={'reason':
                    'QUALIFICATION FIXTURE: assertion 2 + 2 == 5 failed; inspect pinned repository and explain the incorrect assertion.'})
            if role == 'steward':
                from .store.steward_execution import StewardExecutions
                row = StewardExecutions(self.store).reserve(self.command(), mode='recovery',
                    trigger={'failed_job_id':failed['id']},
                    observations={'base_oid':self.base,'observed_at':time.time(),'issues':[]},
                    config=self.config, slot='qualification-steward',
                    execution_fence=str(uuid.uuid4()), launch=self.launch())
            else:
                row = self.life.reserve(self.command(), failed['id'], slot='qualification-recovery',
                                        execution_fence=str(uuid.uuid4()), launch=self.launch())
        else:
            member, candidate, manifest = self.admitted['member_id'], None, self.manifest
            if role != 'build':
                # Local fixture only: base=head, no actual pull request or publication receipt.
                # The reservation still checks exact candidate, generation and SQL lease pins.
                with self.store.write() as db:
                    revision = db.execute('SELECT requirements_hash FROM issue_revisions WHERE id=?',
                                          (self.admitted['revision_id'],)).fetchone()[0]
                    candidate = str(uuid.uuid4())
                    identity = Candidate(1, 'forge/qualification', self.base, self.base,
                                         'qualification-fixture', revision, manifest_hash(self.manifest))
                    db.execute('INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)',
                               (candidate, self.unit, identity.pull_number, identity.branch, identity.head_oid,
                                identity.base_oid, identity.policy_hash, identity.requirements_hash,
                                identity.input_manifest_hash, time.time()))
                    db.execute('UPDATE units SET state=?,current_candidate_id=? WHERE id=?',
                               ('reviewing', candidate, self.unit))
                    if role=='audit':db.execute("UPDATE units SET state='auditing' WHERE id=?",(self.unit,))
                member = None
                manifest = dict(candidate_id=candidate, **{k:v for k,v in asdict(identity).items()
                    if k in {'head_oid','base_oid','policy_hash','requirements_hash','input_manifest_hash'}})
            row = self.life.reserve_job(self.command(), role, slot='qualification-'+role,
                execution_fence=str(uuid.uuid4()), input_manifest=manifest, member_id=member,
                candidate_id=candidate, launch=self.launch(context={'build_result_version':2} if role=='build' else None))
        return ExecutionQueries(self.store).job(row['job_id'])

    def auxiliary(self, role):
        from .store.auxiliary import save_in
        job = dict(id=str(uuid.uuid4()), role=role, repo=self.repository.canonical_name,
            repo_id=self.repository.repository_id, head=self.base, target_branch=self.repository.default_branch,
            publisher='forge-qualification', slot='qualification-'+role, created_at=time.time(),
            deadline=time.time()+self.seconds, runner_config=self.config, fenced=False, cleanup=False)
        if role == 'discussion':
            from .store.discussions import DiscussionStore
            discussions = DiscussionStore(self.store)
            question = 'Qualification only: inspect one tracked source or documentation file and explain its purpose. Do not resolve the issue or propose actions.'
            discussions.send(self.issue_key, question, 'fixture-question', self.admitted['revision_id'], actor='qualification')
            job.update(context=dict(revision=self.admitted['revision_id'], actions=[], issue=self.issue),
                       messages=[dict(role='user', text=question)])
            from .steward_subjects import conversation_dossier
            from .steward_contract import decision_schema
            version=discussions.thread(self.issue_key)['version']
            subject=discussions.steward_subject(self.issue_key,expected_version=version)
            job['steward_mode']='conversation'
            dossier=conversation_dossier(job,subject)
            job['context']={**job['context'],'steward_dossier':dossier,'steward_schema':decision_schema(dossier)}
            discussions.reserve(self.issue_key, job, expected_version=version, origin={})
        elif role == 'triage':
            from .store.repository_planning import RepositoryPlanning
            planning = RepositoryPlanning(self.store)
            proposal = planning.queue(job['repo_id'], request_id='fixture-triage', actor_id='qualification',
                                      base_branch=job['target_branch'])['proposal_id']
            context = planning.context(proposal, head=self.base)
            from .steward_subjects import triage_dossiers, triage_schema
            job.update(slot='repository-triage',proposal_id=proposal,base_branch=job['target_branch'],steward_mode='triage')
            dossiers=triage_dossiers(context,job)
            context.update(steward_dossiers=dossiers,steward_schema=triage_schema(dossiers,context))
            job['context_hash']=manifest_hash(context)
            planning.reserve(proposal, job, context)
            job['context'] = context
        else:
            with self.store.write() as db:
                save_in(db, 'deployment', str(job['repo_id']), job, time.time())
        return job

    def clean_pipeline(self, job, capture):
        self.life.collect(self.command(), job['id'], execution_fence=job['execution_fence'], capture=capture)
        self.life.verify_cleanup(self.command(), job['id'], execution_fence=job['execution_fence'],
                                 evidence={'process_stopped':True, 'resources_released':True})

    def fence(self, job):
        # Revocation in this disposable database, before external teardown. No live store writes.
        with self.store.write() as db:
            for table in ('jobs', 'auxiliary_executions', 'discussion_executions'):
                db.execute(f'UPDATE {table} SET fenced=1 WHERE id=?', (job['id'],))
        job['fenced'] = True
