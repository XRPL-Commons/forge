"""Retire a proven provider-blocked idle generation without changing its work."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import time

from .config import load_repositories
from .control import snapshot_path, atomic_json
from .handoff import Unverified, command, bounded_run, release_command, session_identity
from .human_wait import _locked
from .legacy_health import classify, current_model_limit, current_error_line
from .codex_worker import observe, private_json

OUTAGES = {'blocked_model_usage','blocked_quota','blocked_auth'}


def _marker(path):
    if not path.exists():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
        raise Unverified('startup_marker_unverified')
    data = path.read_bytes()
    return {'sha256':hashlib.sha256(data).hexdigest(),'mtime_ns':info.st_mtime_ns,
            'inode':info.st_ino,'content':data.decode('utf-8')}


def _files(clone, raw):
    entries=raw.split('\0'); paths=set(); index=0
    if len(entries)>513 or len(raw)>65536:
        raise Unverified('dirty_inventory_unbounded')
    while index < len(entries):
        entry=entries[index];index+=1
        if not entry: continue
        if len(entry)<4: raise Unverified('dirty_inventory_unknown')
        paths.add(entry[3:])
        if 'R' in entry[:2] or 'C' in entry[:2]:
            if index>=len(entries): raise Unverified('dirty_inventory_unknown')
            paths.add(entries[index]);index+=1
    result={}; total=0
    for name in sorted(paths):
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise Unverified('dirty_path_unverified')
        path=clone/relative
        if not path.exists() and not path.is_symlink():
            result[name]=None;continue
        info=path.lstat()
        if stat.S_ISLNK(info.st_mode):
            data=os.readlink(path).encode()
        elif stat.S_ISREG(info.st_mode):
            total+=info.st_size
            if total>32*1024*1024: raise Unverified('dirty_content_unbounded')
            with path.open('rb') as stream: data=stream.read(32*1024*1024+1)
            if len(data)>32*1024*1024: raise Unverified('dirty_content_unbounded')
        else:
            raise Unverified('dirty_path_not_regular')
        result[name]={'sha256':hashlib.sha256(data).hexdigest(),'mode':stat.S_IMODE(info.st_mode)}
    return result


def _check(config,state,session,provider,run,now):
    role=session.rsplit('-',1)[-1]
    repos=[r for r in load_repositories(config) if r.enabled and r.short+'-'+role==session]
    if len(repos)!=1 or role not in {'build','review','audit'} or provider not in {'claude','codex'}:
        raise Unverified('handover_identity_unknown')
    repo=repos[0]
    clone=Path(getattr(repo,'clone_'+role) or Path.home()/(repo.short if role=='build' else repo.short+'-'+role)).resolve()
    identity=session_identity(session,run,state=state)
    generation='|'.join(str(identity[k]) for k in ('session_id','session_created','pane_id','pane_pid'))
    if provider=='claude':
        pane=command(['tmux','capture-pane','-e','-p','-t',identity['pane_id']],run)
        reason=classify(pane)
        if reason not in OUTAGES:
            raise Unverified('provider_outage_not_current')
        banner=current_model_limit(pane) or current_error_line(pane)
        outage_proof={'banner_sha256':hashlib.sha256(banner.encode()).hexdigest()}
    else:
        observation=observe(state,session,generation=generation,repo=repo.repo)
        reason={'usage_limit':'blocked_quota','authentication':'blocked_auth'}.get(observation.get('provider_failure'))
        if observation.get('provider')!='codex' or observation.get('reason')!='codex_execution_failed' or not reason:
            raise Unverified('provider_outage_not_current')
        outage_proof={'run_id':observation['run_id'],'thread_id':observation['thread_id']}
    git=['git','-C',str(clone)]
    if Path(command(git+['rev-parse','--show-toplevel'],run)).resolve()!=clone:
        raise Unverified('clone_mismatch')
    remote=command(git+['remote','get-url','origin'],run)
    if remote not in {f'git@github.com:{repo.repo}.git',f'https://github.com/{repo.repo}.git',f'https://github.com/{repo.repo}'}:
        raise Unverified('remote_mismatch')
    head=command(git+['rev-parse','HEAD'],run)
    branch=command(git+['rev-parse','--abbrev-ref','HEAD'],run)
    # Preserve byte-exact status, including filenames containing whitespace.
    result=run(git+['status','--porcelain=v1','-z','--untracked-files=all'],capture_output=True,text=True,timeout=15)
    if result.returncode: raise Unverified('read_failed')
    raw=result.stdout
    files=_files(clone,raw)
    index=hashlib.sha256(command(git+['diff','--cached','--no-ext-diff','--binary'],run).encode()).hexdigest()
    snap=json.loads(snapshot_path(state,repo.repo).read_text())
    at=snap.get('observed_at')
    if (snap.get('repo')!=repo.repo or snap.get('status')!='ok' or snap.get('enabled') is not True
            or type(at) not in (int,float) or not 0<=now-at<=300):
        raise Unverified('snapshot_not_fresh')
    claims=snap.get('schedule',{}).get('active')
    work=snap.get('work')
    if (not isinstance(claims,list) or len(claims)>100 or any(type(n) is not int or n<=0 for n in claims)
            or not isinstance(work,dict)):
        raise Unverified('claim_context_unavailable')
    context={k:work.get(k) for k in ('ready','wip','review','audit','sentback','held','limbo','conflicts')}
    if any(not isinstance(v,list) or len(v)>100 or any(type(n) is not int or n<=0 for n in v) for v in context.values()):
        raise Unverified('claim_context_unavailable')
    marker=_marker(Path(state)/(session+'.up'))
    try:
        guard=json.loads((Path(state)/'legacy-health/guard.json').read_text()).get('sessions',{}).get(session)
    except FileNotFoundError:
        guard=None
    creation_event_id = None
    activity = Path(state)/'activity.sqlite3'
    if activity.exists():
        with sqlite3.connect(activity.absolute().as_uri()+'?mode=ro',uri=True,timeout=1) as db:
            found=db.execute("SELECT id FROM events WHERE session=? AND event='session_created' ORDER BY id DESC LIMIT 1",(session,)).fetchone()
            creation_event_id=found[0] if found else None
    can_clear = False
    if isinstance(guard,dict) and guard.get('reason') == reason:
        if provider == 'claude' and guard.get('provider') in (None,'claude'):
            submitted = marker['content'] if marker else ''
            fingerprint = hashlib.sha256((submitted+'\n'+banner).encode()).hexdigest()
            can_clear = guard.get('error_fingerprint') == fingerprint
        elif provider == 'codex':
            can_clear = guard.get('provider') == 'codex' and guard.get('generation_id') == generation
    if session_identity(session,run,state=state) != identity:
        raise Unverified('handover_generation_changed')
    if provider == 'claude':
        final_pane=command(['tmux','capture-pane','-e','-p','-t',identity['pane_id']],run)
        final_banner=current_model_limit(final_pane) or current_error_line(final_pane)
        if classify(final_pane)!=reason or not final_banner or hashlib.sha256(final_banner.encode()).hexdigest()!=outage_proof['banner_sha256']:
            raise Unverified('provider_outage_changed')
    else:
        final=observe(state,session,generation=generation,repo=repo.repo)
        if (final.get('reason')!='codex_execution_failed' or final.get('run_id')!=outage_proof['run_id']
                or final.get('thread_id')!=outage_proof['thread_id']
                or {'usage_limit':'blocked_quota','authentication':'blocked_auth'}.get(final.get('provider_failure'))!=reason):
            raise Unverified('provider_outage_changed')
    return {'provider':provider,'reason':reason,'session':session,'repo':repo.repo,'role':role,
            'clone':str(clone),'head':head,'branch':branch,'status':raw,'files':files,'index_sha256':index,
            'claims':sorted(claims),'creation_event_id':creation_event_id,'work':context,'snapshot_observed_at':at,
            'startup_marker':marker,'guard':guard,'guard_can_clear':can_clear,'outage_proof':outage_proof,**identity}


def check(config,state,session,provider,*,run=subprocess.run,now=None):
    try:
        evidence=_check(config,state,session,provider,bounded_run(run),time.time() if now is None else now)
        return {'eligible':True,'reason':'verified_provider_handover','failure_class':evidence['reason'],'evidence':evidence}
    except Unverified as exc:
        return {'eligible':False,'reason':str(exc),'evidence':{}}
    except (OSError,ValueError,TypeError,KeyError,AttributeError,sqlite3.Error,subprocess.SubprocessError):
        return {'eligible':False,'reason':'handover_verification_unavailable','evidence':{}}


def release(config,state,session,provider,*,run=subprocess.run,now=None,lock_fd=8):
    if not _locked(state,lock_fd):
        return {'eligible':False,'released':False,'reason':'supervisor_lock_required','evidence':{}}
    action_run=run;run=bounded_run(run,60)
    first=check(config,state,session,provider,run=run,now=now)
    if not first['eligible']: return {**first,'released':False}
    second=check(config,state,session,provider,run=run,now=now)
    if first!=second or not second['eligible'] or not _locked(state,lock_fd):
        return {'eligible':False,'released':False,'reason':'handover_changed','evidence':{}}
    evidence=second['evidence']
    directory=Path(state)/'provider-handovers';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        return {'eligible':False,'released':False,'reason':'handover_manifest_not_private','evidence':{}}
    path=directory/(session+'.json')
    manifest={**evidence,'prepared_at':time.time(),'released':False,
        'instruction':'Resume the existing claims and branch. Preserve all dirty and untracked work; inspect this manifest before claiming new work. Never reset, clean, or discard this checkout.'}
    try:
        private_json(path,manifest)
        release_command(['tmux','kill-session','-t',evidence['session_id']],run,action_run)
    except (OSError,ValueError,subprocess.SubprocessError):
        return {'eligible':False,'released':False,'reason':'handover_release_failed','evidence':{}}
    manifest.update(released=True,released_at=time.time())
    result={'eligible':True,'released':True,'reason':'verified_provider_handover','evidence':{
        'provider':provider,'session_id':evidence['session_id'],'session_created':evidence['session_created'],
        'repo':evidence['repo'],'role':evidence['role'],'head':evidence['head'],'claims':evidence['claims'],
        'manifest':str(path)}}
    try:
        private_json(path,manifest)
        # This compare-and-clear keeps unrelated/account guards and launch charges.
        health=Path(state)/'legacy-health'
        if (health/'guard.json').exists():
            with (health/'guard.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
                guard=json.loads((health/'guard.json').read_text())
                current=guard.get('sessions',{}).get(session)
                if evidence['guard_can_clear'] and current==evidence['guard'] and current and current.get('reason')==evidence['reason']:
                    current.pop('reason',None);current['until']=0
                    atomic_json(health/'guard.json',guard)
                    result['cleared_provider_guard']=True
        marker=Path(state)/(session+'.up')
        if (evidence['startup_marker'] and evidence['startup_marker']['mtime_ns']/1e9>=evidence['session_created']
                and _marker(marker)==evidence['startup_marker']):
            marker.unlink()
            result['cleared_startup_marker']=True
        from .activity import record
        record(state,'session_stopped',session,evidence['repo'],evidence['role'],
               {'reason':'verified_provider_handover'},now=now)
    except (OSError,ValueError,TypeError,KeyError):
        result['receipt_error']=True
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','release'])
    for name in ('config','state','session','provider'): parser.add_argument('--'+name,required=True)
    parser.add_argument('--lock-fd',type=int,default=8)
    args=parser.parse_args()
    result=release(args.config,args.state,args.session,args.provider,lock_fd=args.lock_fd) if args.action=='release' else check(args.config,args.state,args.session,args.provider)
    # Private manifest includes filenames; stdout only exposes bounded reason/identity.
    print(json.dumps({k:result[k] for k in ('eligible','released','reason','failure_class') if k in result},sort_keys=True))
    return 0 if result.get('released',result['eligible']) else 1


if __name__=='__main__':raise SystemExit(main())
