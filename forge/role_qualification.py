"""Explicit native role canaries. No dispatcher, publisher or activation.

The real installed provider runs normal image job.py + harness.py against a real
read-only Git mirror. Only a private disposable database receives fixture work.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile
import time

from .factory.runner import ContainerRunner
from .factory.source_preparation import SourcePreparation
from .mirror.policy import MirrorError
from .role_qualification_fixtures import Fixtures

ROLES = ('build', 'review', 'audit', 'recovery', 'triage', 'strategist', 'discussion', 'steward')
SECONDS = 180


class QualificationError(RuntimeError):
    pass


class QualificationSettings:
    """Read-only policy view for the explicitly selected disposable project.

    Fresh setup has App connections before a repository is enabled. The CLI
    selection supplies this fixture's project identity; live settings still
    supply connections and any project restrictions on every broker request.
    """
    def __init__(self, settings, repository):
        self.settings = settings
        self.database = settings.database
        self.repository = dict(id=repository.repository_id, name=repository.canonical_name, enabled=False)
        self.configured = False
        self.load()

    def load(self):
        config = deepcopy(self.settings.load())
        selected = self.repository
        matches = [row for row in config['repositories']
                   if row['id'] == selected['id'] or row['name'].lower() == selected['name'].lower()]
        if matches:
            if (len(matches) != 1 or matches[0]['id'] != selected['id']
                    or matches[0]['name'].lower() != selected['name'].lower()):
                raise MirrorError('repository_unavailable')
            self.configured = True
        elif self.configured:
            raise MirrorError('repository_unavailable')
        else:
            config['repositories'].append(dict(selected))
        return config


def idle(store):
    """Read current production authority; never pause, drain or change settings."""
    with store.read_snapshot() as db:
        configs = list(db.execute("SELECT domain,revision,settings_json FROM configuration WHERE active=1 AND domain IN ('runtime.json','setup/runtime.json','host-runtime.json') ORDER BY domain"))
        if not configs or any(json.loads(row['settings_json']).get(key) is True
                              for row in configs for key in ('enabled','merge_enabled')):
            raise QualificationError('automation_must_be_disabled')
        for table in ('jobs','auxiliary_executions','discussion_executions'):
            if db.execute(f'SELECT 1 FROM {table} WHERE cleanup=0 LIMIT 1').fetchone():
                raise QualificationError('active_execution_must_finish')
        return [(row['domain'],row['revision']) for row in configs]



def idle_logins(root, config):
    # The SQL login marker is permanent cleanup responsibility, not an active
    # login. Inspect only this installation's actual owned login containers.
    from .harnesses.accounts import owner
    rows=ContainerRunner(config).call(['container','ls','-a','--filter',
        'label=forge.login-root='+owner(root),'--format','{{.ID}}']).stdout
    if rows.strip():raise QualificationError('account_login_in_progress')


def schema_check(value, spec):
    from .harnesses.schema import schema_check as check
    try:
        check(value, spec)
    except ValueError as error:
        raise QualificationError('result_schema_invalid') from error


class CanarySource(SourcePreparation):
    def _notes(self, job, notes, publication_authorization):
        result = super()._notes(job, notes, publication_authorization)
        result['qualification'] = dict(fixture=True, publishes=False,
            candidate='base equals head; synthetic PR identity' if job['role'] in {'review','audit'} else None,
            failure='synthetic incorrect assertion' if job['role'] in {'recovery','steward'} else None)
        evidence_field = ('the recommended choice reason' if job['role']=='steward' else
                          'metadata.reply' if job['role'] in {'triage','discussion'} else 'summary')
        instruction = (
            '\nHOST QUALIFICATION TASK (replaces implementation scope; supplied issue remains context only): '
            'This is a disposable installation canary, not work on the actual GitHub issue. '
            'Read one tracked text source or documentation file in this checkout. Run git rev-parse HEAD and '
            f'git hash-object on that file. Include exactly "QUALIFICATION_READ <path> <blob-oid>" in {evidence_field}, '
            'using the actual path and full Git blob OID. Explain one concrete fact from that file. '
            'Do not install dependencies or run the full project suite. Do not access GitHub directly. ')
        if job['role']=='build':
            instruction += ('Create only forge-qualification.txt containing that observed path and blob OID, '
                'validate its content with a short local check, and commit that file locally. Return build result v2 with '
                'outcome done, claims [], verification null, delegation_plan null and the exact '
                'output_commit with a qualification-only PR title/body. Nothing will be published. ')
        elif job['role'] in {'review','audit'}:
            instruction += 'The fixture candidate has no diff (base=head); verify this and report pass with the exact candidate key. '
        elif job['role']=='recovery':
            instruction += 'Explain why the synthetic assertion 2 + 2 == 5 is incorrect; report pass with evidence and a concrete correction. '
        elif job['role']=='steward':
            instruction += ('Return the normal pinned decision v1 contract with exactly one question choice, '
                'judgement judgement, and brief null. Ask whether to correct the synthetic assertion 2 + 2 == 5. '
                'Explain the incorrect assertion and the observed file fact in the choice reason. '
                'No choice will be selected or applied; no question will be posted. ')
        elif job['role']=='triage':
            instruction += ('This fixture index contains only the selected issue. Return the pinned Steward triage envelope '
                'with exactly one decision containing exactly one keep choice for that issue, brief null, '
                'and metadata with reply describing the file, notes [] and handoff null. No row will be approved. ')
        elif job['role']=='discussion':
            instruction += ('Return the pinned Steward conversation decision with exactly one answer choice and brief null. '
                'Answer the fixture question in metadata.reply, with notes [] and handoff null. No action will be applied. ')
        else:
            instruction += 'Inspect deployment evidence in this checkout and return the normal strategist contract; unknown evidence stays unobservable. '
        result['instructions'] += instruction
        return result


def capture_evidence(capture, job):
    """Only bounded host facts enter the public report; private evidence is separate."""
    from .harnesses.execution import valid
    execution = capture.get('execution') or {}
    facts = {key: execution[key] for key in ('running', 'oom_killed') if type(execution.get(key)) is bool}
    if type(execution.get('exit_code')) is int:
        facts['exit_code'] = execution['exit_code']
    labels = {'valid result artifact': 'result', 'runner logs': 'logs',
        'delegated output bundle': 'bundle', 'valid output receipt': 'output_receipt',
        'valid startup receipt': 'startup_receipt', 'valid execution envelope': 'execution_envelope',
        'container and worker-local evidence': 'container'}
    evidence = dict(role=job['role'], job_id=job['id'], execution=facts,
        result_present=capture.get('result') is not None,
        missing=sorted({labels[label] for label in capture.get('missing', []) if label in labels}))
    envelope = capture.get('execution_envelope')
    if (isinstance(envelope, dict) and valid(envelope, job['id'], final=True)
            and facts.get('running') is False and envelope['exit_code'] == facts.get('exit_code')):
        safe = {'harness_status': envelope['harness_status']}
        if 'provider_error' in envelope:
            safe['provider_error'] = dict(envelope['provider_error'])
        if 'harness_error' in envelope:
            safe['harness_error'] = {'phase': envelope['harness_error']['phase']}
        evidence['execution_envelope'] = safe
    if (execution.get('startup_protocol') == '1' and facts.get('exit_code') == 78
            and facts.get('running') is False and not facts.get('oom_killed')
            and not evidence['result_present'] and not capture.get('timed_out')):
        from .factory.startup_problems import validated_receipt
        startup = validated_receipt(capture.get('forge_startup'), job['id'])
        if startup is not None:
            evidence['startup'] = dict(reason='harness_startup', phase=startup['phase'], exception=startup['exception'])
    return evidence


def validate_capture(capture, job, notes, source):
    from .factory.harness import schema
    from .factory.contracts import result
    from .mirror.git_store import run_git
    evidence = capture_evidence(capture, job)
    execution = evidence['execution']
    envelope = evidence.get('execution_envelope') or {}
    if execution.get('running') is not False or execution.get('exit_code') != 0 or envelope.get('harness_status')!='ok':
        raise QualificationError('provider_execution_not_confirmed')
    value = capture.get('result')
    if value is None:
        raise QualificationError('result_missing')
    schema_check(value, schema(notes))
    if job['role'] in {'build','review','audit','recovery','steward'}:result(deepcopy(value),job)
    if job['role']=='steward':
        if (len(value['choices'])!=1 or value['choices'][0]['action']!='question'
                or value['choices'][0]['judgement']!='judgement' or value['brief'] is not None):
            raise QualificationError('steward_fixture_scope_changed')
        read_evidence=value['choices'][0]['reason']
    elif job['role']=='triage':
        from .steward_subjects import triage_output
        rows,retained,metadata=triage_output(value,job['context']['steward_dossiers'])
        if (len(rows)!=1 or rows[0]['action']!='keep'
                or rows[0]['members']!=[job['context']['issues'][0]['issue_id']]
                or len(retained[0][1]['choices'])!=1 or retained[0][1]['brief'] is not None
                or metadata['notes']!=[] or metadata['handoff'] is not None):
            raise QualificationError('triage_fixture_scope_changed')
        read_evidence=metadata['reply']
    elif job['role']=='discussion':
        from .steward_contract import decision
        decision(value,job['context']['steward_dossier'])
        if (len(value['choices'])!=1 or value['choices'][0]['action']!='answer'
                or value['brief'] is not None or value['metadata']['notes']!=[] or value['metadata']['handoff'] is not None):
            raise QualificationError('discussion_fixture_scope_changed')
        read_evidence=value['metadata']['reply']
    else:
        if value['outcome']!=('done' if job['role']=='build' else 'pass'):
            raise QualificationError('role_did_not_pass')
        if job['role']=='build' and value['verification'] is not None:
            raise QualificationError('build_fixture_scope_changed')
        read_evidence=value['summary']
    if not isinstance(read_evidence,str):raise QualificationError('repository_read_evidence_missing')
    match = re.search(r'QUALIFICATION_READ ([^\s]+) ([0-9a-f]{40}(?:[0-9a-f]{24})?)(?:\s|$)',read_evidence)
    if not match:raise QualificationError('repository_read_evidence_missing')
    path, oid = match.groups()
    if path.startswith('/') or '..' in path.split('/'):raise QualificationError('repository_read_evidence_invalid')
    # Verify evidence against the host cache at the exact assigned base.
    observed = run_git(['--git-dir',str(source.git_store.path(job['repo_id'])), 'rev-parse',
                        job['source_input']['base_oid']+':'+path]).decode().strip()
    kind=run_git(['--git-dir',str(source.git_store.path(job['repo_id'])),'cat-file','-t',oid]).decode().strip()
    if observed != oid or kind!='blob':raise QualificationError('repository_read_evidence_invalid')
    with source.store.read_snapshot() as db:
        receipts=[json.loads(row[0]) for row in db.execute('SELECT details_json FROM mirror_receipts WHERE job_id=?',(job['id'],))]
    if not any(row.get('request_kind')=='upload' and row.get('outcome')=='served' for row in receipts):
        raise QualificationError('mirror_fetch_not_confirmed')
    if job['role']=='build':
        bundle = capture.get('worktree_bundle')
        receipt = capture.get('export_receipt') or {}
        if not bundle or receipt.get('local_head_oid')!=value['output_commit']:
            raise QualificationError('build_export_not_confirmed')
        from .publication_artifacts import inspect_output, BaseObjects
        from .factory.source_inputs import digest
        identity=job['source_input']
        base_objects=BaseObjects(job['repo_id'],digest(identity),identity['object_format'],
            tuple([identity['base_oid'],*identity['input_oids']]),source.git_store.path(job['repo_id']))
        verified=inspect_output(bundle,value['output_commit'],identity,base_objects)
        if (receipt.get('bundle_digest')!=hashlib.sha256(bundle).hexdigest()
                or receipt.get('bundle_size')!=len(bundle) or receipt.get('source_digest')!=digest(identity)
                or receipt.get('job_id')!=job['id']):
            raise QualificationError('build_export_not_confirmed')
        if (len(verified.changes)!=1 or verified.changes[0].path!='forge-qualification.txt'
                or verified.changes[0].old_mode is not None or verified.changes[0].new_mode!='100644'
                or not verified.changes[0].content or len(verified.changes[0].content)>4096
                or path.encode() not in verified.changes[0].content or oid.encode() not in verified.changes[0].content):
            raise QualificationError('build_fixture_scope_changed')
    return dict(role=job['role'],status='qualified',job_id=job['id'],provider=source.config['provider'],
        base_oid=job['source_input']['base_oid'],source_digest=source.mirror.prepared(job['id'])['source_digest'],
        repository_read=dict(path=path,blob_oid=oid),mirror_requests=len(receipts),fixture=True,
        outcome='pass')


def configuration(root, store):
    from .factory.config import load
    config=load(root,scope='active',host_default=True)
    pool=config.get('provider_pool') or []
    ready=config.get('provider_ready')
    available=[entry for entry in pool if ready is None or entry['id'] in ready]
    if pool:
        if not available:raise QualificationError('coding_account_not_ready')
        entry=available[0]
        config.update({k:v for k,v in entry.items() if k!='id'},provider_id=entry['id'])
    return config


def qualify(root, repository, number, *, apply=False, image=None, roles=None):
    from .store.runtime import selected
    from .settings import Settings
    from .credentials import Credentials
    from .factory.github import GitHub
    from .factory.control import controller_lock
    from .mirror_access import MirrorAccess
    from .mirror.git_store import GitStore, run_git
    from .mirror_daemon import MirrorDaemon
    roles = list(ROLES) if roles is None else roles
    if (not isinstance(roles,list) or not roles or any(role not in ROLES for role in roles)
            or len(set(roles))!=len(roles)):
        raise QualificationError('invalid_roles')
    root=Path(root)
    store=selected(root)
    if store is None:raise QualificationError('selected_store_required')
    before=idle(store)
    config=configuration(root,store)
    idle_logins(root,config)
    if image is not None and image!=config['image']:raise QualificationError('installed_image_changed')
    report=dict(status='planned',image=config['image'],repo=repository,issue=number,
        roles=roles,seconds_per_role=SECONDS,max_agent_invocations=len(roles),max_run_seconds=1500,
        source_revision=run_git(['-C',str(Path(__file__).resolve().parent.parent),'rev-parse','HEAD']).decode().strip(),
        provider=config['provider'],model=config.get('model'),automation='disabled',
        fixtures=['candidate base=head, synthetic PR identity','synthetic failed assertion','one-issue triage index'],
        limits=['No issue completion, actual PR review, publication or merge is certified.',
                'Provider usage may incur charges; one agent invocation per role, no retries. Each invocation may make multiple model requests.'])
    if not apply:return report
    reports=[]
    report.update(status='failed',results=reports,cleanup='pending')
    # Blocks competing setup/supervisor ownership for the whole bounded sequence.
    with controller_lock(root):
        if idle(store)!=before:raise QualificationError('configuration_changed')
        idle_logins(root,config)
        settings=Settings(root,scope='active')
        access=MirrorAccess(settings)
        class ReadOnlyGitHub(GitHub):
            def call(self,role,repo,method,suffix,payload=None,**kwargs):
                if method!='GET':raise QualificationError('github_mutation_forbidden')
                return super().call(role,repo,method,suffix,payload,**kwargs)
            def token(self,role,repo_id,**kwargs):
                return super().token(role,repo_id,read_only=True)
        github=ReadOnlyGitHub(settings,config)
        directory=Path(tempfile.mkdtemp(prefix='fq-',dir='/tmp'))
        cleanup=True
        def interrupted(*_):raise QualificationError('qualification_interrupted_or_timed_out')
        old_signals={sig:signal.signal(sig,interrupted) for sig in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT)}
        signal.alarm(1500)
        try:
            resolved=access.resolve(repository)
            fixture_settings=QualificationSettings(settings,resolved)
            issue,requirements,_=github.issue_inputs(dict(repo=repository,repo_id=resolved.repository_id,number=number))
            env={**os.environ,**Credentials(root).environment(only=config['provider_environment']), 'FORGE_CREDENTIAL_ROOT':str(root)}
            cache=GitStore(directory/'cache',credentials=access.credentials,max_cache_bytes=1024**3)
            cache.ensure(resolved)
            base=run_git(['--git-dir',str(cache.path(resolved.repository_id)),'rev-parse','HEAD']).decode().strip()
            for index,role in enumerate(roles):
                report['current_role']=role
                if idle(store)!=before:raise QualificationError('configuration_changed')
                fixtures=Fixtures(directory/str(index),resolved,issue,requirements,base,config,SECONDS)
                source=CanarySource(fixtures.store.root,fixtures.store,settings=fixture_settings,access=access,git_store=cache,github=github)
                source.config=config
                daemon=MirrorDaemon(fixtures.store.root,fixtures.store,settings=fixture_settings,access=access,git_store=cache)
                runner=ContainerRunner(config,environ=env,source=source)
                job=fixtures.reserve(role)
                try:
                    # Prepare immutable notes and bind the broker before starting its network frontend.
                    # ContainerRunner constructs the actual role note on its first prepare_source call.
                    runner.prepare_source(job)
                    daemon.step()
                    runner.create(job)
                    runner.prepare(job)
                    runner.start(job)
                    deadline=time.monotonic()+SECONDS
                    while True:
                        if idle(store)!=before:raise QualificationError('configuration_changed')
                        current=runner.inspect(job)
                        if current and current['running'] is False and current['status']!='created':break
                        if time.monotonic()>=deadline:raise QualificationError('role_deadline_exceeded')
                        time.sleep(1)
                    captured=runner.capture(job)
                    notes=json.loads((source._directory(job['id'])/'assignment.json').read_text())
                    try:
                        reports.append(validate_capture(captured,job,notes,source))
                    except Exception:
                        report['failure']=capture_evidence(captured,job)
                        from .qualification_diagnostics import retain
                        try:
                            diagnostic = retain(root, captured, job, notes,
                                source_revision=report['source_revision'], image=report['image'],
                                secrets=runner._scrub_values(job))
                            if diagnostic:
                                report['diagnostic_id'] = diagnostic
                        except (OSError, ValueError, RuntimeError):
                            # Evidence storage cannot replace the original failure or skip teardown.
                            report['diagnostic_status'] = 'unavailable'
                        raise
                finally:
                    try:
                        fixtures.fence(job)
                        runner.reset(job)
                    except Exception:
                        cleanup=False
                        raise
                    finally:
                        daemon.close()
            if idle(store)!=before:raise QualificationError('configuration_changed')
            report['status']='qualified'
        except Exception as error:
            code=getattr(error,'reason',None)
            report['reason']=(str(error) if isinstance(error,QualificationError) else code
                              if isinstance(code,str) and re.fullmatch('[a-z_]{1,80}',code) else type(error).__name__)
        finally:
            signal.alarm(0)
            for sig,handler in old_signals.items():signal.signal(sig,handler)
            for token,_ in github.tokens.values():
                try:github.client.request('DELETE','/installation/token',token)
                except Exception:cleanup=False
            github.tokens.clear()
            if cleanup:
                # Immutable mount sources become writable only after verified teardown.
                for path in directory.rglob('*'):
                    if path.is_dir():path.chmod(0o700)
                shutil.rmtree(directory)
            report['cleanup']='confirmed' if cleanup else 'unconfirmed'
            if not cleanup:report.update(status='failed',reason='cleanup_unconfirmed',retained_evidence_directory=str(directory))
            try:report['automation_after']='disabled' if idle(store)==before else 'changed'
            except QualificationError:report['automation_after']='changed'
            if report['automation_after']!='disabled':report.update(status='failed',reason='configuration_changed')
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--repo',required=True)
    parser.add_argument('--issue',required=True,type=int)
    parser.add_argument('--image')
    parser.add_argument('--role', choices=ROLES, action='append', dest='roles')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',args.repo) or args.issue<1:raise QualificationError('invalid_repository_or_issue')
        report=qualify(args.root,args.repo,args.issue,apply=args.apply,image=args.image,roles=args.roles)
    except Exception as error:
        report=dict(status='failed',reason=str(error) if isinstance(error,QualificationError) else type(error).__name__)
    print(json.dumps(report))
    return 0 if report['status'] in {'planned','qualified'} else 1


if __name__=='__main__':
    raise SystemExit(main())
