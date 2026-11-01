#!/usr/bin/env python3
"""Load exported release images into isolated classic and containerd Docker stores.

Run with sudo on the Linux CI host. Uses its installed dockerd binary and never
connects to, stops, or changes the builder's Docker daemon.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge import host_update
from forge.release_artifacts import Release, validate_manifest


def daemon_config(root, containerd):
    namespace = 'forge-release-' + uuid.uuid4().hex
    value = {
        'data-root': str(root / 'data'), 'exec-root': str(root / 'exec'),
        'pidfile': str(root / 'docker.pid'), 'hosts': ['unix://' + str(root / 'docker.sock')],
        'containerd-namespace': namespace, 'containerd-plugins-namespace': namespace + '-plugins',
        'bridge': 'none', 'iptables': False, 'ip6tables': False,
        'ip-forward': False, 'ip-masq': False,
        'features': {'containerd-snapshotter': containerd},
    }
    if not containerd:
        value['storage-driver'] = 'overlay2'
    return value


def stop(process):
    # dockerd starts its own containerd. Keep all children within this disposable
    # process group and reap them even if startup or qualification fails.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=15)


@contextmanager
def isolated_daemon(containerd):
    executable = shutil.which('dockerd')
    if executable is None:
        raise RuntimeError('The CI host must already have dockerd installed.')
    with tempfile.TemporaryDirectory(prefix='forge-rt-', dir='/tmp') as directory:
        root = Path(directory)
        config = daemon_config(root, containerd)
        config_path = root / 'daemon.json'
        config_path.write_text(json.dumps(config))
        environment = {key: value for key, value in os.environ.items()
                       if key not in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')}
        def docker(argv, **kwargs):
            if argv[0] != 'docker':
                raise ValueError('Roundtrip qualification accepts Docker commands only.')
            return subprocess.run(['docker', '--host', config['hosts'][0], *argv[1:]],
                                  env=environment, timeout=kwargs.pop('timeout', 600), **kwargs)
        with (root / 'daemon.log').open('w+') as log:
            process = subprocess.Popen([executable, '--config-file', str(config_path)],
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('The isolated Docker daemon stopped during startup.')
                    result = docker(['docker', 'info', '--format', '{{json .}}'],
                                    capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        info = json.loads(result.stdout)
                        driver = dict(info.get('DriverStatus') or []).get('driver-type', '')
                        if (driver == 'io.containerd.snapshotter.v1') != containerd:
                            raise RuntimeError('The isolated daemon did not select the requested image store.')
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('The isolated Docker daemon did not become ready.')
                yield docker
            except BaseException:
                log.flush(); log.seek(0)
                print(log.read()[-8000:], file=sys.stderr)
                raise
            finally:
                stop(process)


def verify_archive(path, descriptor):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if path.stat().st_size != descriptor['size'] or digest.hexdigest() != descriptor['sha256']:
        raise ValueError('The exported ' + path.name + ' archive does not match its release manifest.')


def qualify_store(directory, manifest, docker):
    release = Release(manifest['source'], manifest)
    # The archive is already verified below; make the real image consumer read
    # exactly the export being published, with no cache or network fallback.
    def download(kind, destination):
        archive = directory / manifest['artifacts'][kind]['filename']
        verify_archive(archive, manifest['artifacts'][kind])
        shutil.copyfile(archive, destination)
    release.download = download
    results = {}
    for kind in ('runner', 'full'):
        image = release.image(kind, run=docker)
        docker(['docker', 'run', '--rm', '--pull=never', '--network=none', '--read-only',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=32',
                '--memory=128m', '--entrypoint=/usr/local/bin/python3', image, '-I', '-c',
                'import platform; assert platform.machine() == "x86_64"; print("archive roundtrip passed")'],
               check=True, timeout=60)
        results[kind] = image
    return results


def qualify(directory):
    if platform.system() != 'Linux' or platform.machine() != 'x86_64' or os.geteuid() != 0:
        raise ValueError('Run this qualification with sudo on the Linux amd64 CI host.')
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text())
    inputs = host_update.artifact_inputs()
    # sudo runs as root while checkout belongs to the CI runner. Trust only this
    # explicitly selected checkout, without changing either user's Git config.
    revision = subprocess.check_output(['git', '-c', 'safe.directory=' + str(host_update.SOURCE),
                                        '-C', str(host_update.SOURCE), 'rev-parse', 'HEAD'], text=True).strip()
    validate_manifest(manifest, revision, inputs, host_update.NODE_IMAGE, host_update.PYTHON_IMAGE)
    for containerd in (False, True):
        name = 'containerd' if containerd else 'classic'
        print('Qualifying exported images in the ' + name + ' Docker store…', flush=True)
        with isolated_daemon(containerd) as docker:
            result = qualify_store(directory, manifest, docker)
        print(json.dumps({'store': name, 'images': result, 'status': 'passed'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    qualify(parser.parse_args().assets)
