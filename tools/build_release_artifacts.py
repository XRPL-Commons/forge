#!/usr/bin/env python3
"""Build and qualify release assets for the exact checked-out commit (Linux amd64)."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from forge import host_update
from forge.release_artifacts import ASSETS, release_tag, validate_manifest


def descriptor(path, inputs, image=None):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    result = {'filename': path.name, 'sha256': digest.hexdigest(), 'size': path.stat().st_size, 'inputs': inputs}
    if image:
        result.update(image)
    return result


def image_identity(path, built_image):
    """Pin config and manifest identities from the actual exported image.

    Classic Docker identifies an image by its config; containerd uses its
    manifest. Both identities must describe the same exported configuration.
    Read small JSON metadata in one pass without extracting image layers.
    """
    metadata = {}
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            if not member.isfile() or member.size > 256 * 1024:
                continue
            if member.name not in ('index.json', 'manifest.json') and not re.fullmatch(r'blobs/sha256/[0-9a-f]{64}', member.name):
                continue
            raw = archive.extractfile(member).read()
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            metadata[member.name] = (raw, value)
    try:
        entries = metadata['manifest.json'][1]
        index = metadata['index.json'][1]
        if len(entries) != 1 or len(index['manifests']) != 1:
            raise ValueError('Prepared workers require one exported image.')
        config_raw, config = metadata[entries[0]['Config']]
        config_id = 'sha256:' + hashlib.sha256(config_raw).hexdigest()
        manifest_id = index['manifests'][0]['digest']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest_id):
            raise ValueError('Invalid exported manifest digest.')
        manifest_raw, manifest = metadata['blobs/sha256/' + manifest_id.removeprefix('sha256:')]
        if ('sha256:' + hashlib.sha256(manifest_raw).hexdigest() != manifest_id
                or manifest['config']['digest'] != config_id
                or entries[0]['Config'] != 'blobs/sha256/' + config_id.removeprefix('sha256:')
                or config['os'] != 'linux' or config['architecture'] != 'amd64'
                or built_image not in (config_id, manifest_id)):
            raise ValueError('Exported worker identity differs from the qualified image.')
    except (KeyError, TypeError, IndexError) as error:
        raise ValueError('Exported worker metadata is incomplete.') from error
    return {'image_id': config_id, 'image_manifest_id': manifest_id}


def save_image(image, destination):
    with destination.open('wb') as output, gzip.GzipFile(fileobj=output, mode='wb', mtime=0, compresslevel=6) as compressed:
        process = subprocess.Popen(['docker', 'image', 'save', image], stdout=subprocess.PIPE)
        try:
            shutil.copyfileobj(process.stdout, compressed, 1024 * 1024)
            if process.wait():
                raise RuntimeError('Docker image export failed')
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill(); process.wait()


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
        with tarfile.open(destination / ASSETS['ui'], 'w:gz') as archive:
            for child in sorted(ui.iterdir()):
                archive.add(child, arcname=child.name)
    save_image(base, destination / ASSETS['runner'])
    save_image(full, destination / ASSETS['full'])
    identities = {kind: image_identity(destination / ASSETS[kind], image)
                  for kind, image in (('runner', base), ('full', full))}
    inputs = host_update.artifact_inputs()
    manifest = {'version': 1, 'source': revision, 'platform': 'linux/amd64', 'node_image': host_update.NODE_IMAGE,
                'python_image': host_update.PYTHON_IMAGE,
                'artifacts': {kind: descriptor(destination / filename, inputs[kind], identities.get(kind))
                              for kind, filename in ASSETS.items()}}
    validate_manifest(manifest, revision, inputs, host_update.NODE_IMAGE, host_update.PYTHON_IMAGE)
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    print('Prepared and qualified artifacts for ' + revision)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output)
