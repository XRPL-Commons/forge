#!/usr/bin/env python3
"""Build and qualify release assets for the exact checked-out commit (Linux amd64)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge import host_update
from forge.release_artifacts import ASSETS, release_tag, validate_manifest
from tools.ui_dependency_notices import write_notices

RELEASE_MANIFEST_VERSION = 2


def descriptor(path, inputs):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    result = {'filename': path.name, 'sha256': digest.hexdigest(), 'size': path.stat().st_size, 'inputs': inputs}
    return result


def build(destination):
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise ValueError('Prepared releases are built on Linux amd64.')
    revision = host_update.source_revision()
    release_tag(revision)
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=host_update.SOURCE).strip():
        raise ValueError('Release source must be a clean checkout of the tested commit.')
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError('Release output directory must be empty; existing assets are never overwritten.')
    os.environ['FORGE_BUILD_FROM_SOURCE'] = '1'
    base = host_update.runner_image()
    full = host_update.full_runner_image(base)
    with tempfile.TemporaryDirectory(prefix='forge-release-build-') as temporary:
        root = Path(temporary)
        source_policy = root / 'docker-seccomp.json'
        with urllib.request.urlopen(host_update.SECCOMP_URL, timeout=30) as response:
            source_policy.write_bytes(response.read(1024 * 1024))
        generate = runpy.run_path(str(host_update.SOURCE / 'deploy/runner/seccomp_profile.py'))['generate']
        policy = root / 'runner-seccomp.json'
        policy.write_text(json.dumps(generate(source_policy, host_update.SECCOMP_SHA)))
        apparmor = host_update.SOURCE / 'deploy/runner' / (host_update.APPARMOR_PROFILE + '.apparmor')
        subprocess.run(['sudo', 'apparmor_parser', '-r', str(apparmor)], check=True)
        host_update.qualify_runner(base, policy, host_update.APPARMOR_PROFILE)
        host_update.qualify_runner(full, policy, host_update.APPARMOR_PROFILE, full=True)
        ui = root / 'ui/releases' / host_update.ui_release(root)
        write_notices(root / 'ui/build', ui / 'public/third-party-notices.txt', runtime=ui / 'server/node_modules')
        with tarfile.open(destination / ASSETS['ui'], 'w:gz') as archive:
            for child in sorted(ui.iterdir()):
                archive.add(child, arcname=child.name)
    inputs = host_update.artifact_inputs()
    manifest = {'version': RELEASE_MANIFEST_VERSION, 'source': revision, 'platform': 'linux/amd64', 'node_image': host_update.NODE_IMAGE,
                'python_image': host_update.PYTHON_IMAGE,
                'artifacts': {'ui': descriptor(destination / ASSETS['ui'], inputs['ui'])},
                'workers': {kind: {'delivery': 'local-build', 'inputs': inputs[kind]}
                            for kind in ('runner', 'full')}}
    validate_manifest(manifest, revision, inputs, host_update.NODE_IMAGE, host_update.PYTHON_IMAGE)
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    print('Prepared and qualified artifacts for ' + revision)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output)
