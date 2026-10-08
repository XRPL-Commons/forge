"""Offline acceptance of the public SQLite projections against a sealed source.

Only this diagnostic verifier materializes old source files, in a private
throwaway directory. Runtime adapters read neither that copy nor import archives.
Reports name mismatching fields and identities; they never include their values.
"""
from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace

from .snapshot import Snapshot, MigrationError


class NoNetwork:
    def __getattr__(self, name):
        raise RuntimeError('Public parity cannot make a network request')


@contextmanager
def materialized(snapshot):
    with tempfile.TemporaryDirectory(prefix='forge-public-parity-') as directory:
        root=Path(directory)
        root.chmod(0o700)
        for name in snapshot.entries:
            target=root/name
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            # Snapshot safe_name rejects path traversal before this copy.
            target.write_bytes(snapshot.read(name));target.chmod(0o600)
        yield root


@contextmanager
def legacy_views(snapshot,ledger,now):
    # This is an immutable diagnostic copy. Legacy readers otherwise parse the
    # entire transcript-bearing ledger for every single issue detail.
    from unittest.mock import patch
    from ...factory.ledger import Ledger
    from ...factory import issues, control
    import time
    frozen=SimpleNamespace(time=lambda:now,monotonic=time.monotonic)
    inventory=snapshot.json('state/issues/inventory.json',{})
    present=issues.presentation;cache={}
    original_copy=issues.deepcopy
    # Legacy view changes only active/active_role/cleanup_pending on its issue
    # copy; nested histories are read-only. Keep those top-level writes private
    # while copying each frozen nested history just once.
    frozen_items={id(item):original_copy(item) for item in ledger.get('issues',{}).values()}
    def copy_issue(value):
        return dict(frozen_items[id(value)]) if id(value) in frozen_items else original_copy(value)
    def presentation(issue,item,repo,runtime,prs=(),*,fresh=True):
        identity=(repo['id'],issue['number'],fresh)
        if identity not in cache:cache[identity]=present(issue,item,repo,runtime,prs,fresh=fresh)
        return cache[identity]
    with patch.object(Ledger,'read',lambda self:ledger), patch.object(issues.IssueInventory,'read',lambda self:inventory), \
         patch.object(issues,'time',frozen), patch.object(control,'time',frozen), patch.object(issues,'presentation',presentation), \
         patch.object(issues,'deepcopy',copy_issue):
        yield


def verify(snapshot,store):
    """Return a read-only acceptance report; any unexplained mismatch blocks."""
    from ...factory.store_views import PipelineViews
    from ...factory.issues import IssueInventory
    from ...factory import control
    from ...factory.config import validate as runtime_validate
    from ...settings import Settings
    from ...sqlite_settings import SqliteSettings
    from ..configuration import Configuration
    from . import parity, supporting_parity, operational_parity, identity_parity, configuration, context
    snapshot=snapshot if isinstance(snapshot,Snapshot) else Snapshot(snapshot)
    now=snapshot.manifest['captured_at']
    findings=[];counts={};approved={}
    def mismatch(domain,identity,field):
        counts['mismatches']=counts.get('mismatches',0)+1
        fields=counts.setdefault('mismatched_fields',{});fields[domain+'.'+field]=fields.get(domain+'.'+field,0)+1
        if len(findings)<100:findings.append({'domain':domain,'identity':str(identity),'field':field,'code':'public_fact_mismatch'})
    def equal(domain,identity,field,old,new):
        counts['comparisons']=counts.get('comparisons',0)+1
        if old!=new:mismatch(domain,identity,field)
    def allow(rule):approved[rule]=approved.get(rule,0)+1
    adapter=SimpleNamespace(source=snapshot,store=store)
    # These independent read checks cover every normalized source fact, all
    # artifact chunks and the identity authorization decisions at capture time.
    for name,checker in [('durable',parity.check),('supporting',supporting_parity.check),
                         ('operational',operational_parity.check),('identity',identity_parity.check),
                         ('configuration',configuration.check),('context',context.check)]:
        try:
            result=checker(adapter)
            counts.update({name+':'+key:value for key,value in result.items()})
        except (MigrationError,ValueError,KeyError,TypeError) as error:
            findings.append({'domain':name,'code':getattr(error,'code','normalized_facts_unavailable')})
    ledger=snapshot.json('state/factory/factory-ledger.json',{})
    with materialized(snapshot) as root, legacy_views(snapshot,ledger,now):
        try:
            old_settings=Settings(root,client=NoNetwork(),clock=lambda:now)
            new_settings=SqliteSettings(root,client=NoNetwork(),clock=lambda:now);new_settings.database=store
            equal('settings','instance','configuration',old_settings.load(),new_settings.load())
            equal('settings','instance','access',old_settings.observation(),new_settings.observation())
            old_public=old_settings.view();new_public=new_settings.view()
            equal('settings','instance','public_view',old_public,new_public)
            runtime=Configuration(store).get('runtime.json')
            runtime=runtime_validate(runtime['settings'] if runtime else {})
            views=PipelineViews(store,repositories=new_public['repositories'],runtime=runtime,clock=lambda:now)
            old_inventory=IssueInventory(root,settings=old_settings,github=NoNetwork(),clock=lambda:now)
            # Legacy control.view never calls GitHub. The explicit throwaway
            # tree prevents it from selecting or mutating live runtime state.
            old_factory=control.view(root)
            equal('factory','instance','runtime',old_factory['runtime'],views.factory(section='board')['runtime'])
            for old in old_factory['issues']:
                key=old['key']
                page=views.factory(issue=key)
                if len(page['issues'])!=1:
                    mismatch('factory',key,'visible_issue');continue
                new=page['issues'][0]
                for field in ('key','repo_id','repo','number','title','active','failure_count','created_at','closed_as','reviewers_requested','last_summary','blocked_dependencies'):
                    expected=old.get(field);actual=new.get(field)
                    if field=='blocked_dependencies':expected=expected or [];actual=actual or []
                    if field=='reviewers_requested':
                        expected={k:expected[k] for k in ('logins','at') if k in expected} if expected else None
                        actual={k:actual[k] for k in ('logins','at') if k in actual} if actual else None
                    # Legacy summary omits source creation time; it remains a
                    # public fact in the issue itself and must survive import.
                    if field=='created_at':expected=ledger['issues'][key].get('created_at')
                    equal('factory',key,field,expected,actual)
                candidate=old.get('candidate') or {}
                current=new.get('candidate') or {}
                for field in ('pr','head','merge_commit'):
                    if field in candidate:equal('candidate',key,field,candidate[field],current.get(field))
                old_events=[{k:e[k] for k in ('at','kind','provider','model') if k in e} for e in old.get('history',[])]
                equal('factory',key,'history',old_events,new.get('history',[]))
                for field in ('deployed','deploy_failed'):
                    equal('factory',key,field,old.get(field),new.get(field))
                if old.get('state')!=new.get('state'):
                    # #209 intentionally adds a fail-closed reconciliation gate
                    # while imported assignments receive fresh observations.
                    if new.get('reason_code') in {'legacy_reconciliation','membership_decision','external_change'}:
                        allow('approved_import_reconciliation_display')
                    elif new.get('lifecycle',{}).get('state')=='done' and old.get('closed_as')=='completed':
                        allow('approved_closed_completed_is_done')
                    else:mismatch('factory',key,'state')
            # Inventory includes untracked issues absent from the delivery list.
            expected=old_inventory.view(state='all',page=1)
            for page_number in range(1,expected['pages']+1):
                old_page=expected if page_number==1 else old_inventory.view(state='all',page=page_number)
                for old in old_page['issues']:
                    key=old['key']
                    old_detail=old_inventory.view(key=key)['issue']
                    new=views.issues(key=key)['issue']
                    for field in ('key','repo_id','repo','number','title','body','body_truncated','state','state_reason','updated_at','labels','assignees','attempts'):
                        equal('issue',key,field,bool(old_detail.get(field)) if field=='body_truncated' else old_detail.get(field),new.get(field))
                    equal('issue',key,'history',old_detail.get('history',[]),new.get('history',[]))
                    for field in ('approvals','held_by','environment_blocker','decision_request','approval','last_checked','merge_blocker','active'):
                        equal('issue',key,field,old_detail.get(field),new.get(field))
                    for field in ('assignee_types','label_events'):
                        empty={} if field=='assignee_types' else []
                        equal('issue',key,field,old_detail.get(field,empty),new.get(field,empty))
                    for field in ('content_hash','setup_url'):
                        if field in old_detail:equal('issue',key,field,old_detail[field],new.get(field))
                    prior=old_detail.get('verification_request');current=new.get('verification_request')
                    equal('issue',key,'verification_request',prior,{k:v for k,v in current.items() if k not in {'gate_id','can_confirm'}} if current else None)
                    for field in ('choice','override','full_available'):
                        equal('issue',key,'environment.'+field,old_detail.get('environment',{}).get(field),new.get('environment',{}).get(field))
                    equal('issue',key,'reviews',sorted(old_detail.get('reviews',[]),key=lambda row:row['role']),sorted(new.get('reviews',[]),key=lambda row:row['role']))
                    expected=old_detail.get('reviewers_requested');actual=new.get('reviewers_requested')
                    equal('issue',key,'reviewers_requested',expected,{k:actual[k] for k in ('logins','at') if k in actual} if actual else None)
                    # Explicitly reviewed canonical presentation differences.
                    # These fields describe the new lifecycle and authority;
                    # contentful worker/operator facts are checked above.
                    canonical={'status','reason_code','ledger_state','explanation','summary','actions','closable','setup_needed','fresh','revision'}
                    checked={'key','repo_id','repo','number','title','body','body_truncated','state','state_reason','updated_at','labels','assignees','attempts','history','approvals','held_by','environment_blocker','decision_request','approval','last_checked','merge_blocker','active','assignee_types','label_events','verification_request','environment','reviews','reviewers_requested','dependencies','pull_requests','reviewers','default_reviewers','content_hash','setup_url'}
                    for field in set(old_detail)-checked-canonical:mismatch('issue',key,'unclassified:'+field)
                    if any(old_detail.get(field)!=new.get(field) for field in canonical):allow('approved_canonical_lifecycle_presentation')

                    for field in ('dependencies','pull_requests','reviewers','default_reviewers'):
                        prior=old_detail.get(field,[]);current=new.get(field,[])
                        sort_key=(lambda row:row['number']) if field=='pull_requests' else (lambda row:row['login'].lower()) if field=='reviewers' else None
                        equal('issue',key,field,sorted(prior,key=sort_key),sorted(current,key=sort_key))
                    counts['public_issues']=counts.get('public_issues',0)+1
                    if old_detail.get('status')!=new.get('status') or old_detail.get('actions')!=new.get('actions'):
                        if new.get('reason_code') in {'legacy_reconciliation','membership_decision','external_change'} and not new['actions']:
                            allow('approved_import_reconciliation_actions')
                        elif not new['fresh'] and not new['actions']:
                            allow('approved_stale_observation_withholds_actions')
                        else:mismatch('issue',key,'status_or_actions')
            for identifier,old in ledger.get('jobs',{}).items():
                new=views.job(identifier)
                for field in ('id','role','phase','outcome','cleanup','fenced','created_at','slot','provider','provider_id','model','deadline'):
                    if field in old:equal('job',identifier,field,old[field],new.get(field))
                expected=(old.get('capture') or old.get('observation') or {}).get('logs','')
                if expected:
                    equal('output',identifier,'logs',expected,views.output(identifier)['logs'])
                counts['public_jobs']=counts.get('public_jobs',0)+1
        except (ValueError,KeyError,TypeError,OSError,RuntimeError) as error:
            # Do not expose configuration secrets or worker text from errors.
            findings.append({'domain':'public_views','code':'projection_unavailable','error_type':type(error).__name__})
    return {'status':'blocked' if findings else 'passed','findings':findings,'counts':counts,
            'approved_differences':[{'rule':key,'count':value} for key,value in sorted(approved.items())]}
