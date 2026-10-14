"""Trusted setup probes use fixed image programs and never receive GitHub authority."""
import hashlib
import json
import os
from pathlib import Path

from .contracts import assignment_note
from .runner import ContainerRunner, RunnerError
from .worker_boundary import BoundarySpec, WorkerBoundary

PROVIDER = ['/usr/local/bin/python3', '-I', '/opt/forge/probe.py']
TERMINATION = ['/usr/local/bin/python3', '-c', 'import os,time; p=os.fork(); os.setsid() if p==0 else None; time.sleep(120)']


class ProbeRunner(ContainerRunner):
    def source_record(self, job):
        return None

    def identity(self, job):
        if (job.get('slot') != 'setup-probe' or job.get('publisher') != 'setup-probe'
                or job.get('role') != 'build' or job.get('repo') != 'example/setup'
                or job.get('unit_id') or job.get('kind') not in ('provider', 'termination', 'reuse')
                or self.config['harness_argv'] != (PROVIDER if job['kind'] == 'provider' else TERMINATION)):
            raise RunnerError('Only persisted setup probes may use the trusted probe runner')
        job.setdefault('execution_fence', 'setup:' + job['id'])
        return WorkerBoundary.identity(job)

    def inputs(self, job):
        self.identity(job)
        root = self.environ.get('FORGE_CREDENTIAL_ROOT')
        if not root:
            raise RunnerError('Setup probes require their installation-owned input directory')
        parent = Path(root) / 'state/setup-probe-inputs'
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o077:
            raise RunnerError('Setup probe input directory is not private')
        return parent / hashlib.sha256(job['id'].encode()).hexdigest()

    def prepare_source(self, job):
        name, labels = self.identity(job)
        path = self.inputs(job)
        note = assignment_note(job)
        note.update(harness_argv=self.config['harness_argv'], grace_seconds=self.config['grace_seconds'],
                    proxy='http://forge-egress:3128', provider=self.config.get('provider', 'codex'),
                    model=self.config.get('model'), boundary_environment={})
        raw = json.dumps(note, sort_keys=True).encode()
        if path.exists():
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
                raise RunnerError('Setup probe inputs changed')
            target = path / 'assignment.json'
            if target.is_symlink() or target.read_bytes() != raw:
                raise RunnerError('Setup probe assignment changed')
        else:
            path.mkdir(mode=0o700)
            fd = os.open(path / 'assignment.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            (path / 'assignment.json').chmod(0o444)
            path.chmod(0o555)
        boundary = WorkerBoundary(self.call, image=self.config['image'], work_bytes=256*1024**2)
        volume, keeper = name + '-probe-work', name + '-probe-keeper'
        boundary.volume(volume, labels)
        boundary.endpoint(keeper, labels, 'none', ['-c', 'import time;time.sleep(86400)'],
                          ['type=volume,source='+volume+',target=/retained-work,readonly,volume-nocopy'], [])
        return BoundarySpec(self.config['network'], volume, keeper, '',
                            ('--network', self.config['network'], '--mount', 'type=volume,source='+volume+',target=/work,volume-nocopy',
                             '--mount', 'type=bind,source='+str(path)+',target=/run/forge-input,readonly'), {})

    def prepare(self, job, *, bundles=()):
        self.identity(job)
        if bundles:
            raise RunnerError('Setup probes cannot receive repository bundles')
        return self._prepare_credentials(job)

    def reset(self, job):
        name, labels = self.identity(job)
        result = super().reset(job)
        boundary = WorkerBoundary(self.call, image=self.config['image'], work_bytes=256*1024**2)
        targets = [('container', name+'-probe-keeper'), ('volume', name+'-probe-work')]
        for kind, target in targets:
            observed = boundary.inspect(kind, target)
            if observed:
                boundary.owned(observed, labels, container=kind == 'container')
        for kind, target in targets:
            if boundary.inspect(kind, target):
                self.call([kind, 'rm', *(['--force'] if kind == 'container' else []), target])
        if any(boundary.inspect(kind, target) for kind, target in targets):
            raise RunnerError('Setup probe volume cleanup is unconfirmed')
        path = self.inputs(job)
        if path.exists():
            if path.is_symlink() or path.stat().st_uid != os.geteuid():
                raise RunnerError('Setup probe input ownership changed')
            saved = json.loads((path / 'assignment.json').read_text())
            if saved.get('id') != job['id']:
                raise RunnerError('Setup probe input identity changed')
            path.chmod(0o700)
            (path / 'assignment.json').unlink()
            path.rmdir()
        return result
