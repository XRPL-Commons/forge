#!/usr/bin/env python3
"""Real-engine reset using a private temporary installation and synthetic jobs only."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from forge.factory.config import validate
from forge.factory.runner import ContainerRunner
from forge.settings import write_json
from forge.setup_service import Service
from forge.state import StateError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    args=parser.parse_args()
    config=validate({'image':args.image,'apparmor_profile':None,'seccomp_profile_env':None,
                     'provider_environment':[],'grace_seconds':1})
    with tempfile.TemporaryDirectory(prefix='forge-reset-canary-') as folder:
        root=Path(folder);service=Service(root)
        job=service.new_probe(config,'termination')
        runner=ContainerRunner(job['runner_config'])
        jobs=[job]
        try:
            service.setup.read()
            with service.setup.store.transaction() as data: data['probe_jobs']=[job]
            job['container_id']=runner.create(job)
            runner.prepare(job,'synthetic-no-github-access');runner.start(job)
            time.sleep(2)
            assert runner.inspect(job)['running']
            write_json(root/'factory.json',{'version':1,'sync_interval_seconds':300,'access_max_age_seconds':900,'connections':{},'repositories':[]})
            service.setup.queue('reset',generation=service.setup.read()['generation']); stale=service.setup.read()
            for _ in range(4):
                Service(root).tick()
                if service.setup.read()['phase']=='setup':break
            assert service.setup.read()['phase']=='setup',service.setup.read().get('error')
            assert runner.inspect(job) is None
            assert not (root/'factory.json').exists()
            assert list((root/'archives').glob('*/receipt.json'))
            fresh=service.new_probe(config,'termination');jobs.append(fresh)
            fresh['container_id']=runner.create(fresh);runner.prepare(fresh,'synthetic');runner.start(fresh)
            try: service.reset(stale)
            except StateError: pass
            else: raise AssertionError('Stale reset accepted')
            assert runner.inspect(fresh)['running']
            print('Real onboarding reset passed: restart recovery, teardown, archive, new generation, stale reset isolation.')
        finally:
            for job in jobs:
                job['fenced']=True;runner.reset(job)


if __name__=='__main__':main()
