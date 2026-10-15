"""Durable full worker environment installation, before repository activation."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from . import host_update
from .configuration_authority import RUNTIME_DOMAINS
from .store.runtime import required
from forge_client.contracts import digest, require

SERVICES = ['mongodb', 'postgres', 'redis']
KEY = 'environment_installation'


class Environment:
    def __init__(self, root, setup_id):
        self.root, self.setup_id = Path(root), setup_id
        require(isinstance(setup_id, str) and re.fullmatch('[a-f0-9]{32}', setup_id))
        self.store = required(root)
        self.unit = 'forge-install-environment-' + setup_id

    @contextmanager
    def locked(self, *, wait=True):
        with (self.root / 'state/install-environment.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            yield

    def identity(self):
        record = host_update.Record(self.root).data
        with self.store.read_snapshot() as db:
            row = db.execute("SELECT generation,phase FROM setup_instances WHERE scope='live'").fetchone()
        require(row is not None, 'setup_required', 'Inspect the factory setup before installing its environment.')
        base = record.get('image')
        require(isinstance(base, str) and re.fullmatch('sha256:[a-f0-9]{64}', base), 'installation_required',
                'Install the standard runner before its database environment.')
        engine = subprocess.run(['docker', 'version', '--format', '{{.Server.Version}}'],
                                check=True, capture_output=True, text=True, timeout=20).stdout.strip()
        source = host_update.source_revision()
        require(isinstance(source, str) and re.fullmatch('[a-f0-9]{40}', source), 'source_required')
        return {'setup_id': self.setup_id, 'instance': self.store.instance_id, 'generation': row['generation'],
                'source_revision': source, 'base_image': base, 'engine_version': engine,
                'inputs': host_update.tree_digest(('deploy/runner/Dockerfile.full', 'deploy/runner/services-canary.cjs'), base),
                'seccomp_sha256': hashlib.sha256((self.root / 'runner-seccomp.json').read_bytes()).hexdigest(),
                'apparmor_profile': host_update.APPARMOR_PROFILE,
                'apparmor_sha256': hashlib.sha256(host_update.APPARMOR_TARGET.read_bytes()).hexdigest()}

    def installed(self, image):
        if not image:
            return False
        result = subprocess.run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                                capture_output=True, text=True, timeout=20)
        return result.returncode == 0 and result.stdout.strip() == image

    def running(self):
        result = subprocess.run(['systemctl', '--user', 'is-active', self.unit],
                                capture_output=True, text=True, timeout=15)
        return result.stdout.strip() in ('active', 'activating', 'deactivating', 'reloading')

    def save(self, receipt, **values):
        # Re-read unrelated host state at every CAS; never overwrite an earlier snapshot.
        host_update.Record(self.root).save(**{KEY: receipt, **values})

    def configured(self, image):
        values = [host_update.runtime_snapshot(self.root, name) for name in RUNTIME_DOMAINS]
        return bool(any(values)) and all(row is None or
            row['settings'].get('environment_tiers', {}).get('full', {}).get('image') == image for row in values)

    def view(self):
        identity = self.identity()
        record = host_update.Record(self.root).data
        saved = record.get(KEY)
        matches = saved and saved.get('binding') == digest(identity)
        image = saved.get('image') if matches else None
        complete = bool(matches and saved.get('status') == 'complete' and record.get('full_image') == image
                        and self.installed(image) and self.configured(image))
        status = ('environment_ready' if complete else 'environment_installing' if self.running()
                  else 'environment_interrupted' if saved else 'environment_required')
        return {'status': status, 'tier': 'full', 'services': SERVICES, 'identity': identity,
                'image': image, 'receipt': saved if matches else None}

    def apply(self):
        try:
            return self._apply()
        except BlockingIOError:
            return {**self.view(), "status": "environment_installing"}

    def _apply(self):
        with self.locked(wait=False):
            observed = self.view()
            if observed['status'] in ('environment_ready', 'environment_installing'):
                return observed
            with self.store.read_snapshot() as db:
                row = db.execute("SELECT phase FROM setup_instances WHERE scope='live'").fetchone()
            require(row and row['phase'] == 'setup', 'setup_complete',
                    'Install the database environment before activating the factory.')
            binding = digest(observed['identity'])
            saved = observed['receipt']
            if not saved or saved.get('binding') != binding:
                self.save({'binding': binding, 'status': 'pending', 'identity': observed['identity'],
                           'image': None, 'qualified_at': None})
            subprocess.run(['systemd-run', '--user', '--quiet', '--collect', '--unit=' + self.unit,
                            '--property=Type=exec', '--property=UMask=0077', '--property=RuntimeMaxSec=1800',
                            '--working-directory=' + str(host_update.SOURCE),
                            sys.executable, '-m', 'forge.launch', 'forge.install_environment',
                            '--root', str(self.root), '--setup-id', self.setup_id, '--worker', binding],
                           check=True, capture_output=True, timeout=30)
            return {**observed, 'status': 'environment_installing'}

    def worker(self, binding):
        with self.locked():
            identity = self.identity()
            require(digest(identity) == binding, 'setup_changed')
            receipt = host_update.Record(self.root).data.get(KEY)
            require(receipt and receipt['binding'] == binding, 'setup_changed')
            try:
                image = receipt.get('image')
                if not receipt.get('qualified_at') or not self.installed(image):
                    receipt.update(image=None, qualified_at=None, status='building')
                    self.save(receipt)
                    image = host_update.full_runner_image(identity['base_image'])
                    host_update.qualify_runner(image, self.root / 'runner-seccomp.json',
                                               host_update.APPARMOR_PROFILE, full=True)
                    require(digest(self.identity()) == binding, 'setup_changed')
                    receipt.update(image=image, qualified_at=time.time(), status='qualified')
                    self.save(receipt)
                # Re-check after the expensive build, then let the existing host
                # updater serialize the switch with supervisor dispatch.
                from .sqlite_identity import identity_effect_lock
                with identity_effect_lock(self.store):
                    require(digest(self.identity()) == binding, 'setup_changed')
                    with self.store.read_snapshot() as db:
                        phase = db.execute("SELECT phase FROM setup_instances WHERE scope='live'").fetchone()[0]
                    require(phase == 'setup', 'setup_changed')
                    host_update.switch_full_environment(self.root, image)
                    receipt.update(status='complete')
                    self.save(receipt, full_image=image)
            except Exception:
                receipt.update(status='interrupted')
                self.save(receipt)
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--setup-id', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--worker')
    args = parser.parse_args()
    environment = Environment(args.root, args.setup_id)
    if args.worker:
        environment.worker(args.worker)
    else:
        print(json.dumps(environment.apply() if args.apply else environment.view(), allow_nan=False))


if __name__ == '__main__':
    main()
