#!/usr/bin/env python3
"""Opt-in, credential-free real-engine reset canary. Touches only fresh test IDs."""
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import uuid

from forge.factory.config import validate
from forge.factory.runner import ContainerRunner


def main():
    suffix = uuid.uuid4().hex
    network, tag = 'forge-contract-' + suffix, 'forge-contract:' + suffix
    def docker(*args, **kwargs):
        return subprocess.run(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', *args],
                              check=True, capture_output=True, timeout=180, **kwargs)
    archive = io.BytesIO()
    files = {'Dockerfile': b'FROM python:3.12-slim\nRUN mkdir /work && chown 1000:1000 /work\nCOPY job.py /opt/forge/job.py\nUSER 1000:1000\n',
             'job.py': (Path(__file__).resolve().parents[1] / 'forge/factory/job.py').read_bytes()}
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name); info.size = len(content); tar.addfile(info, io.BytesIO(content))
    docker('build', '--label', 'forge.contract-test=' + suffix, '-t', tag, '-', input=archive.getvalue())
    image = json.loads(docker('image', 'inspect', tag).stdout)[0]['Id']
    docker('network', 'create', '--internal', '--label', 'forge.contract-test=' + suffix, network)
    config = validate({'image': image, 'network': network, 'provider_environment': [], 'apparmor_profile': None, 'seccomp_profile_env': None, 'grace_seconds': 1,
                       'harness_argv': ['/usr/local/bin/python3', '-c', 'import os,time; p=os.fork(); os.setsid() if p==0 else None; time.sleep(120)']})
    runner = ContainerRunner(config)
    jobs = []
    try:
        runner.ready()
        for n in range(2):
            job = {'id': str(uuid.uuid4()), 'slot': 'build-1', 'role': 'build', 'repo': 'example/project',
                   'repo_id': 1, 'issue_number': 1, 'publisher': 'fixture[bot]', 'target_branch': 'main', 'branch': 'fixture', 'candidate': None, 'history': {},
                   'deadline': time.time() + 120, 'fenced': False}
            jobs.append(job)
            job['container_id'] = runner.create(job)
            runner.prepare(job, 'synthetic-no-github-access')
            runner.start(job)
            assert runner.inspect(job)['running']
            capture = runner.capture(job)
            assert capture['result'] is None
            # A new external controller instance can reset without local worker state.
            job['fenced'] = True
            recovered = ContainerRunner(config)
            recovered.reset(job)
            assert recovered.inspect(job) is None
            recovered.reset(job)  # Idempotent reset.
            print('Verified clean reset and slot reuse', n + 1, flush=True)
        # The old identity cannot touch the replacement, even while it is live.
        fresh = {**jobs[-1], 'id': str(uuid.uuid4()), 'fenced': False}
        fresh.pop('container_id'); jobs.append(fresh)
        fresh['container_id'] = runner.create(fresh); runner.prepare(fresh, 'synthetic'); runner.start(fresh)
        runner.reset(jobs[0]); assert runner.inspect(fresh)['running']
        print('Verified stale reset leaves replacement running', flush=True)
    finally:
        for job in jobs:
            job['fenced'] = True
            runner.reset(job)
        docker('network', 'rm', network)
        docker('image', 'rm', tag)
    print('Container lifecycle canary passed; no coding harness or real credentials used.')


if __name__ == '__main__':
    main()
