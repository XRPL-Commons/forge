"""Finish one already-revoked setup probe through normal verified teardown.

Called only by an authorized upgrade after source/tool qualification. This does
not launch checks, fence active work, or replace the ordinary drain blockers.
"""
import time

from .. import upgrade as schema


def cleanup_fenced_probe(root, store):
    observed=schema.inspect(store)
    if observed['current_schema']!=observed['target_schema']:
        # Current adapters must never interpret an older execution schema.
        return 0
    from ..db import Store
    from ...sqlite_identity import identity_effect_lock
    from .drain import controller_lease
    from ...onboarding import SetupState
    from ...factory.probe_runner import ProbeRunner
    from ...credentials import Credentials
    database=Store.open(store.root)
    # Matches setup's identity -> controller order; no transaction spans Docker.
    with identity_effect_lock(database), controller_lease(root):
        state=SetupState(root,database,time.time)
        with state.transaction() as data:
            for job in data.get('probe_jobs',[]):
                if not job.get('fenced') or job.get('cleanup'):
                    continue
                with database.read_snapshot() as db:
                    row=db.execute('SELECT domain,owner_id,fenced,cleanup FROM auxiliary_executions WHERE id=?',
                                   (job['id'],)).fetchone()
                if row is None or tuple(row)!=('setup','live',1,0):
                    continue
                runner=ProbeRunner(job['runner_config'],environ=Credentials(root).environment(only=[]))
                runner.reset(job)  # Exact labels, worker absence, volume and input removal.
                job['cleanup']=True
                # SetupState atomically retains manifest, history and operation;
                # save_in releases its existing lease only after verified reset.
                return 1
    return 0
