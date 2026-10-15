"""Finish fenced deployments and relinquish never-started auxiliary workers.

The bundled source is qualified before entry. Admission is disabled and the
controller lease prevents a start between physical inspection and durable fence.
Unfenced running/completed workers keep their ordinary collection and drain path.
"""
from copy import deepcopy

from .. import upgrade as schema


REASON = 'maintenance_interrupted'


def unstarted(job, current, *, interrupted=False):
    if job.get('capture') is not None:
        return False
    if current is None:
        # A known container that disappeared may have run. Only our own durable
        # interruption can explain its absence on a teardown retry.
        return not job.get('container_id') or interrupted
    return (current.get('status') == 'created' and current.get('running') is False
            and 'started_at' in current and current['started_at'] is None)


def cleanup_unstarted_auxiliary(root, store):
    observed = schema.inspect(store)
    if observed['current_schema'] != observed['target_schema']:
        return 0
    from ..db import Store
    from ..auxiliary import job_in
    from ..repository_planning import RepositoryPlanning
    from ...credentials import Credentials
    from ...deployments import Deployments
    from ...factory.runner import ContainerRunner
    from ...sqlite_identity import identity_effect_lock
    from .drain import controller_lease, Drain
    database = Store.open(store.root)
    cleaned = 0
    with identity_effect_lock(database), controller_lease(root):
        with database.read_snapshot() as db:
            _, config, _ = Drain.current(db)
            if config.get('enabled', False) is not False:
                return 0
            rows = [dict(row) for row in db.execute("SELECT id,domain,owner_id FROM auxiliary_executions "
                "WHERE cleanup=0 AND domain IN ('deployment','triage') ORDER BY created_at,id LIMIT 50")]
        plans = RepositoryPlanning(database)
        # Teardown needs source authority and engine access, never provider
        # secrets or a renewal/model invocation.
        def runner_for(config):
            return ContainerRunner(config, environ=Credentials(root).environment(only=[]))
        deployments = Deployments(root, runner=runner_for, github=None, settings=None)
        for row in rows:
            with database.read_snapshot() as db:
                job = job_in(db, row['id'])
            if row['domain'] == 'triage':
                interrupted = plans.maintenance_interrupted(job['id'])
            else:
                entry = deployments.read()['repositories'].get(row['owner_id'])
                if not entry or not entry.get('job') or entry['job']['id'] != job['id']:
                    continue
                interrupted = entry.get('reason') == REASON
                if job['fenced'] and job.get('capture') is not None:
                    # Collection already finished; the ordinary fenced path can
                    # verify teardown and settle the saved result without launch.
                    deployments.drive(row['owner_id'])
                    cleaned += 1
                    continue
            if job['fenced'] and not interrupted:
                continue
            runner = runner_for(job['runner_config'])
            current = runner.inspect(job)
            if not unstarted(job, current, interrupted=interrupted):
                continue
            if row['domain'] == 'triage':
                job = plans.interrupt_for_maintenance(job['id'])
            else:
                job['fenced'] = True
                deployments.save(row['owner_id'], job=job, reason=REASON, reread_requested=True)
            # reset may enrich its in-memory job with source metadata. Those
            # observations must not replace the immutable launch manifest.
            runner.reset(deepcopy(job))
            if row['domain'] == 'triage':
                plans.interrupt_for_maintenance(job['id'], cleanup=True)
            else:
                job['cleanup'] = True
                deployments.save(row['owner_id'], job=job, status='idle', reason=REASON,
                                 reread_requested=True, observation=None, error=None,
                                 summary='Maintenance interrupted this worker before it started.')
            cleaned += 1
    return cleaned
