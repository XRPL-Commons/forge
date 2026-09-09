"""Explicit operator requests, executed only by the locked supervisor.

Stop interrupts input and pauses that exact generation. Release delegates to
existing repeated clean/publication/quiescence proofs; it never forces cleanup.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from .config import load_repositories
from .control import atomic_json
from .lifecycle import generation as parse_generation, observe_generation
from .human_wait import _locked


class Refused(ValueError):pass


def _directory(state, create=False):
    path=Path(state)/'slot-actions'
    if create:path.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.exists() and (path.is_symlink() or not stat.S_ISDIR(path.stat().st_mode) or path.stat().st_mode & 0o077):
        raise Refused('action_directory_not_private')
    return path


def _read(path):
    if path.is_symlink() or path.stat().st_size>32768:raise Refused('action_record_invalid')
    value=json.loads(path.read_text())
    if not isinstance(value,dict):raise Refused('action_record_invalid')
    return value


def _repository(config, session):
    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+-(build|review|audit)',session):
        raise Refused('invalid_action_session')
    rows=[(r,role) for r in load_repositories(config) for role in ('build','review','audit') if r.short+'-'+role==session]
    if len(rows)!=1:raise Refused('configured_worker_required')
    return rows[0]


def _assignment(state, session):
    from .workflow import read_state
    value=read_state(state)
    slot=next((s for s in value['slots'] if s['session']==session),None)
    a=next((a for a in value['assignments'] if slot and a['id']==slot.get('assignment_id')),None)
    return {k:a.get(k) for k in ('id','repo','role','session','issue_numbers','pr','generation')} if a else None


@contextmanager
def _queue(state):
    directory=_directory(state,True)
    with (directory/'.queue-lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        pending=directory/'pending';pending.mkdir(mode=0o700,exist_ok=True)
        if pending.is_symlink() or pending.stat().st_mode & 0o077:raise Refused('action_directory_not_private')
        marker=directory/'.pending-indexed'
        if not marker.exists():
            # Upgrade legacy flat queues once; terminal history cannot starve them.
            for path in directory.glob('*.json'):
                if not re.fullmatch(r'[0-9a-f]{32}\.json',path.name):continue
                row=_read(path)
                if row.get('status') in {'queued','running'}:_index(directory,row)
            atomic_json(marker,{'schema':1})
        yield directory


def _index(directory,row):
    session=row.get('session')
    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+-(build|review|audit)',session):
        raise Refused('invalid_action_session')
    if not re.fullmatch('[0-9a-f]{32}',row.get('id','')):raise Refused('action_record_invalid')
    target=directory/'pending'/session;target.mkdir(mode=0o700,parents=True,exist_ok=True)
    if target.is_symlink() or target.stat().st_mode & 0o077:raise Refused('action_directory_not_private')
    atomic_json(target/(row['id']+'.json'),{'id':row['id'],'session':session})


def _pending(directory,session):
    target=directory/'pending'/session
    if not target.exists():return []
    if target.is_symlink():raise Refused('action_directory_not_private')
    rows=[]
    for index in target.glob('*.json'):
        if not re.fullmatch(r'[0-9a-f]{32}\.json',index.name):raise Refused('action_record_invalid')
        row=_read(directory/index.name)
        if row.get('session')!=session or row.get('id')!=index.stem:raise Refused('action_record_invalid')
        if row.get('status') in {'queued','running'}:rows.append(row)
        else:index.unlink(missing_ok=True)
    return sorted(rows,key=lambda row:(row['requested_at'],row['id']))


def request(config,state,action,session,generation,*,run=subprocess.run,now=None):
    if action not in {'stop','release','resume'}:raise Refused('invalid_slot_action')
    repo,role=_repository(config,session)
    if not parse_generation(generation):raise Refused('invalid_action_generation')
    actual=observe_generation(session,run=run)
    if not actual or actual['id']!=generation:raise Refused('action_generation_changed')
    assignment=_assignment(state,session)
    if assignment and assignment.get('generation')!=generation:raise Refused('action_assignment_generation_changed')
    now=time.time() if now is None else now
    with _queue(state) as directory:
        pending=_pending(directory,session)
        same=next((r for r in pending if r['action']==action and r['generation']==generation),None)
        if same:return {'ok':True,'id':same['id'],'reason':'queued','deduplicated':True}
        if len(pending)>=4:raise Refused('too_many_pending_actions')
        identifier=uuid.uuid4().hex
        value={'schema':1,'id':identifier,'action':action,'session':session,'generation':generation,
               'repo':repo.repo,'role':role,'assignment':assignment,'requested_at':now,'updated_at':now,
               'status':'queued','reason':'queued','message':'Operator '+action+' request queued.'}
        atomic_json(directory/(identifier+'.json'),value);_index(directory,value)
    return {'ok':True,'id':identifier,'reason':'queued'}


def read_actions(state):
    directory=_directory(state)
    if not directory.exists():return []
    # Read no more than 100 JSON documents. Pending work uses its own index.
    paths=[p for p in directory.glob('*.json') if re.fullmatch(r'[0-9a-f]{32}\.json',p.name)]
    paths.sort(key=lambda p:(p.stat().st_mtime_ns,p.name),reverse=True)
    rows=[]
    for path in paths[:100]:
        row=_read(path)
        if row.get('id')!=path.stem or row.get('schema')!=1:raise Refused('action_record_invalid')
        rows.append(row)
    return sorted(rows,key=lambda row:(row.get('updated_at',row['requested_at']),row['id']),reverse=True)


def is_paused(state,session,*,run=subprocess.run):
    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+-(build|review|audit)',session):
        raise Refused('invalid_action_session')
    path=_directory(state)/('pause-'+session+'.json')
    if not path.exists():return False
    value=_read(path);actual=observe_generation(session,run=run)
    if actual is None:raise Refused('pause_generation_unavailable')
    return bool(value.get('session')==session and value.get('paused') is True and actual and actual['id']==value.get('generation'))


def _release(config,state,session,role,lock_fd,run):
    from . import handoff, human_wait
    try:
        from . import blocked_work
    except ImportError:
        blocked_work=None
    checks=[lambda:handoff.release(config,state,session,lock_fd=lock_fd,run=run)]
    if role=='audit':
        repo,_=_repository(config,session)
        checks.append(lambda:human_wait.release(config,state,repo.repo,session=session,lock_fd=lock_fd,run=run))
    if role=='build' and blocked_work is not None:checks.append(lambda:blocked_work.release(config,state,session,lock_fd=lock_fd,run=run))
    from .execution_retirement import release as retire_execution
    checks.append(lambda:retire_execution(config,state,session,lock_fd=lock_fd,run=run))
    reasons=[]
    for check in checks:
        value=check()
        if value.get('released') is True:return value
        reasons.append(value.get('reason','release_unverified'))
    return {'released':False,'reason':'release_proof_required','proof_reasons':reasons}


def process(config,state,session,*,lock_fd=8,run=subprocess.run,now=None):
    if not _locked(state,lock_fd):raise Refused('supervisor_lock_required')
    now=time.time() if now is None else now
    outcomes=[]
    if not _directory(state).exists():return []
    _repository(config,session)
    with _queue(state) as directory:pending=_pending(directory,session)[:4]
    for row in pending:
        if row.get('session')!=session or row.get('status') not in {'queued','running'}:continue
        path=_directory(state)/(row['id']+'.json')
        def finish(status,reason,message):
            row.update(status=status,reason=reason,message=message,updated_at=time.time() if now is None else now)
            with _queue(state):
                atomic_json(path,row)
                (_directory(state)/'pending'/session/(row['id']+'.json')).unlink(missing_ok=True)
            outcomes.append(dict(row))
        try:
            if row['status']=='running':raise Refused('interrupted_action_requires_reconciliation')
            if not 0<=now-row['requested_at']<=300:raise Refused('action_expired')
            repo,role=_repository(config,session)
            actual=observe_generation(session,run=run)
            if repo.repo!=row['repo'] or role!=row['role'] or not actual or actual['id']!=row['generation']:
                raise Refused('action_generation_changed')
            if _assignment(state,session)!=row.get('assignment'):raise Refused('action_assignment_changed')
            if not _locked(state,lock_fd):raise Refused('supervisor_lock_required')
            row.update(status='running',reason='executing',updated_at=now);atomic_json(path,row)
            if row['action']=='stop':
                pause={'schema':1,'id':row['id'],'session':session,'generation':row['generation'],
                       'paused':True,'reason':'operator_stopped','observed_at':now}
                atomic_json(_directory(state)/('pause-'+session+'.json'),pause)
                row['paused']=True
                # The exact pane ID cannot retarget a replacement named session.
                pane=row['generation'].split('|')[2]
                result=run(['tmux','send-keys','-t',pane,'C-c'],capture_output=True,text=True,timeout=5)
                if result.returncode:raise Refused('interrupt_delivery_uncertain')
                finish('succeeded','operator_stopped','Interrupt sent; this worker generation is paused. No task completion inferred.')
            elif row['action']=='resume':
                with _queue(state) as directory:
                    pause=directory/('pause-'+session+'.json')
                    if not pause.exists():raise Refused('matching_pause_required')
                    saved=_read(pause);observed=observe_generation(session,run=run)
                    if (saved.get('session')!=session or saved.get('generation')!=row['generation']
                            or saved.get('paused') is not True or not observed or observed['id']!=row['generation']):
                        raise Refused('pause_generation_changed')
                    pause.unlink()
                row['paused']=False
                finish('succeeded','operator_resumed','Generation pause cleared; normal supervisor safety and dispatch checks still apply.')
            elif row['action']=='release':
                def fenced_run(args,**kwargs):
                    if args[:2]==['tmux','kill-session']:
                        if args!=['tmux','kill-session','-t',row['generation'].split('|')[0]]:
                            raise Refused('release_generation_changed')
                        observed=observe_generation(session,run=run)
                        if not observed or observed['id']!=row['generation']:
                            raise Refused('release_generation_changed')
                    return run(args,**kwargs)
                result=_release(config,state,session,role,lock_fd,fenced_run)
                if result.get('released'):
                    row['release_reason']=result.get('reason')
                    finish('succeeded','slot_released','Verified idle worker released; issue state and preserved work retained.')
                else:
                    row['proof_reasons']=result.get('proof_reasons',[])
                    finish('refused','release_proof_required','Worker retained: clean published-work and idle release proof is required. No force release performed.')
            else:raise Refused('invalid_slot_action')
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
            reason=str(exc) if isinstance(exc,Refused) else 'action_verification_unavailable'
            finish('refused' if isinstance(exc,Refused) else 'failed',reason,reason.replace('_',' '))
    return outcomes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['process','paused'])
    parser.add_argument('--state',required=True);parser.add_argument('--session',required=True)
    parser.add_argument('--config');parser.add_argument('--lock-fd',type=int,default=8)
    args=parser.parse_args()
    try:
        if args.action=='paused':return 0 if is_paused(args.state,args.session) else 1
        if not args.config:parser.error('--config is required for process')
        print(json.dumps(process(args.config,args.state,args.session,lock_fd=args.lock_fd)));return 0
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        print(json.dumps({'ok':False,'reason':str(exc) if isinstance(exc,Refused) else 'action_verification_unavailable'}));return 2


if __name__=='__main__':raise SystemExit(main())
