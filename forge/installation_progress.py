"""Credential-free CLI progress, bound to one host setup generation."""
import math
import re
import time

from forge_client.contracts import fields, require
from .store.configuration import Configuration
from .store.runtime import required


def publish(root, snapshot, *, setup_id, generation, clock=time.time):
    fields(snapshot, ('version', 'started_at', 'updated_at', 'runner', 'state', 'stages'))
    require(snapshot['version'] == 1 and snapshot['runner'] == 'cli', 'invalid_progress')
    require(snapshot['state'] in ('running', 'waiting', 'stopped', 'complete'), 'invalid_progress')
    def timestamp(value):
        require(type(value) in (int, float) and math.isfinite(value) and value > 0, 'invalid_progress')
    timestamp(snapshot['started_at']); timestamp(snapshot['updated_at'])
    require(isinstance(snapshot['stages'], list) and len(snapshot['stages']) <= 80, 'invalid_progress')
    stages = []
    for stage in snapshot['stages']:
        require(isinstance(stage, dict) and {'id', 'label', 'status'} <= set(stage)
                and not set(stage) - {'id', 'label', 'status', 'started_at', 'completed_at', 'reason', 'error'}, 'invalid_progress')
        require(isinstance(stage['id'], str) and re.fullmatch(r'[a-z0-9_.-]{1,100}', stage['id']), 'invalid_progress')
        require(stage['status'] in ('pending', 'running', 'waiting', 'complete', 'failed'), 'invalid_progress')
        for key in ('label', 'reason', 'error'):
            if key in stage:
                require(isinstance(stage[key], str) and 0 < len(stage[key]) <= 300
                        and all(c.isprintable() for c in stage[key]), 'invalid_progress')
        for key in ('started_at', 'completed_at'):
            if key in stage:
                timestamp(stage[key])
        stages.append(dict(stage))
    require(len({stage['id'] for stage in stages}) == len(stages), 'invalid_progress')
    store = required(root); configuration = Configuration(store, clock=clock)
    prior = configuration.get('installation_progress', generation)
    require(prior is None or prior['settings']['setup_id'] == setup_id, 'setup_changed')
    with store.write() as db:
        from .store.runtime import writable
        writable(db)
        current = db.execute("SELECT generation,phase FROM setup_instances WHERE scope='live'").fetchone()
        require(current and current['generation'] == generation and current['phase'] in ('setup', 'active'), 'setup_changed')
        value = {**snapshot, 'stages': stages, 'setup_id': setup_id, 'updated_at': clock()}
        configuration.replace_in(db, 'installation_progress', value, subject=generation,
                                 expected_revision=prior['revision'] if prior else 0)
    return {'status': 'progress_saved'}
