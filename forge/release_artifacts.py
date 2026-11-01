"""Prepared release assets from the Forge repository, verified before use.

The publisher is the same GitHub repository trusted for Forge source. A manifest
binds each asset's checksum, build inputs and image ID to one exact source commit.
Host-specific runner qualification still runs after an image is loaded.
"""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request

REPOSITORY = 'XRPL-Commons/forge'
ASSETS = {'ui': 'forge-ui-linux-amd64.tar.gz', 'runner': 'forge-runner-linux-amd64.tar.gz',
          'full': 'forge-runner-full-linux-amd64.tar.gz'}
SHA256 = re.compile(r'[0-9a-f]{64}')
IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')


class ArtifactError(ValueError):
    pass


def source_build():
    value = os.environ.get('FORGE_BUILD_FROM_SOURCE', '')
    if value not in ('', '0', '1'):
        raise ArtifactError('FORGE_BUILD_FROM_SOURCE must be 0 or 1.')
    return value == '1'


def require(value, message):
    if not value:
        raise ArtifactError(message)


def release_tag(revision):
    require(isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{40}', revision),
            'Prepared artifacts require an exact source commit. For a development checkout, explicitly set FORGE_BUILD_FROM_SOURCE=1.')
    return 'forge-build-' + revision


def asset_url(revision, name):
    return 'https://github.com/' + REPOSITORY + '/releases/download/' + release_tag(revision) + '/' + name


def validate_manifest(value, revision, inputs, node_image, python_image):
    require(isinstance(value, dict) and set(value) == {'version', 'source', 'platform', 'node_image', 'python_image', 'artifacts'},
            'Invalid prepared artifact manifest.')
    require(value['version'] == 1 and value['source'] == revision and value['platform'] == 'linux/amd64'
            and value['node_image'] == node_image and value['python_image'] == python_image,
            'Prepared artifacts do not match this source revision, platform or pinned toolchain.')
    require(isinstance(value['artifacts'], dict) and set(value['artifacts']) == set(ASSETS), 'Incomplete prepared release.')
    for kind, asset in value['artifacts'].items():
        keys = {'filename', 'sha256', 'size', 'inputs'} | ({'image_id'} if kind != 'ui' else set())
        require(isinstance(asset, dict) and set(asset) == keys and asset['filename'] == ASSETS[kind], 'Invalid prepared asset descriptor.')
        require(isinstance(asset['sha256'], str) and SHA256.fullmatch(asset['sha256'])
                and type(asset['size']) is int and 0 < asset['size'] < 2 * 1024**3,
                'Invalid prepared asset checksum or size.')
        require(asset['inputs'] == inputs[kind], 'Prepared ' + kind + ' build inputs differ from the checked-out source. '
                'Use a clean published revision or explicitly set FORGE_BUILD_FROM_SOURCE=1 for development.')
        if kind != 'ui':
            require(isinstance(asset['image_id'], str) and IMAGE_ID.fullmatch(asset['image_id']), 'Invalid prepared runner image ID.')
    return value


def read_manifest(revision):
    try:
        with urllib.request.urlopen(asset_url(revision, 'manifest.json'), timeout=30) as response:
            raw = response.read(128 * 1024 + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise ArtifactError('Prepared artifacts are unavailable for ' + str(revision) + '. Wait for its Prepared release workflow '
                            'or select a published release. Development builds require explicit FORGE_BUILD_FROM_SOURCE=1.') from error
    require(len(raw) <= 128 * 1024, 'Prepared artifact manifest is too large.')
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise ArtifactError('Invalid prepared artifact manifest JSON.') from error


@lru_cache(maxsize=4)
def prepared(revision, runner_inputs, full_inputs, ui_inputs, node_image, python_image):
    require(platform.system() == 'Linux' and platform.machine() in ('x86_64', 'amd64'),
            'Prepared Forge artifacts currently support Linux amd64. Other development targets require explicit FORGE_BUILD_FROM_SOURCE=1.')
    manifest = read_manifest(revision)
    inputs = {'runner': runner_inputs, 'full': full_inputs, 'ui': ui_inputs}
    return Release(revision, validate_manifest(manifest, revision, inputs, node_image, python_image))


class Release:
    def __init__(self, revision, manifest):
        self.revision, self.manifest = revision, manifest

    def download(self, kind, destination):
        asset = self.manifest['artifacts'][kind]
        digest, size = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(asset_url(self.revision, asset['filename']), timeout=120) as response, open(destination, 'wb') as target:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    require(size <= asset['size'], 'Prepared asset exceeds its declared size.')
                    digest.update(chunk)
                    target.write(chunk)
        except (urllib.error.URLError, TimeoutError) as error:
            raise ArtifactError('Could not download prepared ' + kind + ' artifact. Resume setup to retry.') from error
        require(size == asset['size'] and digest.hexdigest() == asset['sha256'], 'Prepared ' + kind + ' artifact checksum mismatch.')

    def image(self, kind, *, run=subprocess.run):
        image = self.manifest['artifacts'][kind]['image_id']
        inspect = ['docker', 'image', 'inspect', image, '--format', '{{.Id}} {{.Os}}/{{.Architecture}}']
        found = run(inspect, capture_output=True, text=True)
        if found.returncode:
            print('Downloading the prepared ' + ('full worker' if kind == 'full' else 'worker') + ' image…', flush=True)
            with tempfile.TemporaryDirectory(prefix='forge-release-') as temporary:
                archive = Path(temporary) / ASSETS[kind]
                self.download(kind, archive)
                run(['docker', 'image', 'load', '--input', str(archive)], check=True)
            found = run(inspect, capture_output=True, text=True)
        require(found.returncode == 0 and found.stdout.strip() == image + ' linux/amd64',
                'Loaded runner differs from the prepared image ID or platform.')
        return image

    def ui(self, target):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        print('Downloading the prepared operator UI…', flush=True)
        with tempfile.TemporaryDirectory(prefix='.prepared-', dir=target.parent) as temporary:
            archive = Path(temporary) / ASSETS['ui']
            staging = Path(temporary) / 'output'
            self.download('ui', archive)
            staging.mkdir()
            # Python's data filter rejects traversal and links outside the release.
            with tarfile.open(archive, 'r:gz') as bundle:
                bundle.extractall(staging, filter='data')
            require((staging / 'server/index.mjs').is_file(), 'Prepared UI has no server entry point.')
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging, target)
