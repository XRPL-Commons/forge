"""Resume only preserved builder claims from an unconsumed verified handover.

The caller must first prove the session absent. This is local admission demand,
never authority to claim another issue, terminate a worker, or discard a checkout.
"""
import argparse
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .config import load_repositories
from .control import snapshot_path


def numbers(value):
    if (not isinstance(value,list) or len(value)>100 or
            any(type(n) is not int or n<=0 for n in value) or len(set(value))!=len(value)):
        raise ValueError('Invalid claim list')
    return set(value)


def read_demand(config,state,session,*,now=None):
    empty={'demand':0,'claims':[],'reason':'no_verified_handover_demand'}
    try:
        if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',session) or not session.endswith('-build'):
            return empty
        matches=[r for r in load_repositories(config) if r.enabled and r.short+'-build'==session]
        if len(matches)!=1:
            return empty
        repo=matches[0].repo
        root=Path(state)
        path=root/'provider-handovers'/(session+'.json')
        if path.is_symlink() or path.stat().st_size>1024*1024:
            return empty
        manifest=json.loads(path.read_text())
        now=time.time() if now is None else now
        released=manifest.get('released_at')
        if (manifest.get('released') is not True or manifest.get('session')!=session or
                manifest.get('repo')!=repo or manifest.get('role')!='build' or
                type(released) not in (int,float) or not math.isfinite(released) or not 0<=released<=now):
            return empty
        event_id=manifest.get('creation_event_id')
        if type(event_id) is not int or event_id<=0:
            return empty
        activity=root/'activity.sqlite3'
        if not activity.exists() or activity.is_symlink():
            return empty
        db=sqlite3.connect(activity.absolute().as_uri()+'?mode=ro',uri=True,timeout=1)
        try:
            row=db.execute("SELECT id FROM events WHERE session=? AND event='session_created' ORDER BY id DESC LIMIT 1",(session,)).fetchone()
        finally:
            db.close()
        if not row or row[0]!=event_id:
            return {**empty,'reason':'handover_already_consumed_or_unknown_generation'}
        snapshot=json.loads(snapshot_path(state,repo).read_text())
        observed=snapshot.get('observed_at')
        if (snapshot.get('repo')!=repo or snapshot.get('status')!='ok' or snapshot.get('enabled') is not True or
                type(observed) not in (int,float) or not math.isfinite(observed) or not 0<=now-observed<=300):
            return {**empty,'reason':'snapshot_not_fresh'}
        schedule=snapshot['schedule']
        # Handed-off WIP labels are not permission to restart building published PRs.
        claims=numbers(manifest['claims']) & numbers(schedule['active']) & numbers(snapshot['work']['wip'])
        claims-=numbers(schedule['handed_off'])
        return {'demand':len(claims),'claims':sorted(claims),'reason':'preserved_claims_need_worker' if claims else 'no_remaining_builder_claims'}
    except (OSError,ValueError,TypeError,KeyError,AttributeError,sqlite3.Error):
        return empty


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('config','state','session'):
        parser.add_argument('--'+key,required=True)
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args()
    value=read_demand(args.config,args.state,args.session)
    print(json.dumps(value,sort_keys=True) if args.json else value['demand'])
    return 0


if __name__=='__main__':
    raise SystemExit(main())
