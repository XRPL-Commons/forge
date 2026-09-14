#!/usr/bin/env python3
"""Opt-in authenticated setup probe. One small model request; no GitHub access."""
import argparse
from pathlib import Path
import tempfile
import time

from forge.factory.config import validate
from forge.factory.runner import ContainerRunner
from forge.setup_service import Service
from forge.settings import write_json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',required=True);p.add_argument('--seccomp',required=True)
    p.add_argument('--auth-file',required=True)
    args=p.parse_args()
    config=validate({'image':args.image,'provider_environment':[], 'provider_auth_file_env':'FORGE_SETUP_PROVIDER_AUTH'})
    with tempfile.TemporaryDirectory(prefix='forge-provider-probe-') as folder:
        service=Service(folder);job=service.new_probe(config,'provider')
        runner=ContainerRunner(job['runner_config'],environ={'PATH':'/usr/bin:/bin','FORGE_RUNNER_SECCOMP':str(Path(args.seccomp).resolve()),
                                                          'FORGE_SETUP_PROVIDER_AUTH':str(Path(args.auth_file).resolve())})
        try:
            write_json(Path(folder)/'job.json',job)
            runner.ready();job['container_id']=runner.create(job)
            write_json(Path(folder)/'job.json',job)
            runner.prepare(job,'synthetic-no-github-access');runner.start(job)
            while runner.inspect(job)['running'] and time.time()<job['deadline']:
                time.sleep(2)
            capture=runner.capture(job)
            diagnostics=[text for text in ('Native sandbox probe failed','Provider authentication or model check failed','FileNotFoundError','PermissionError','TimeoutExpired','provider_login_expired','provider_connection_failed') if text in capture.get('logs','')]
            assert capture['execution']['exit_code']==0 and capture['result']=={'ready':True,'attempt':job['id']}, 'Probe failed: '+', '.join(diagnostics or ['result unavailable'])
            print('Authenticated provider and native sandbox probe passed; no issue or PR was touched.')
        finally:
            job['fenced']=True;write_json(Path(folder)/'job.json',job)
            runner.reset(job)
            print('Probe container and disposable authentication copy removed.')


if __name__=='__main__':main()
