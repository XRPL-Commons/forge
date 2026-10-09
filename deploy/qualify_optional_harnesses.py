#!/usr/bin/env python3
"""Offline command/protocol spike for pinned OpenCode and Muse distributions.

Downloads are a separate operator step. This does not install or qualify an adapter.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent / 'provider_qualification'


def verify_artifacts(opencode, muse, platform, pins):
    expected = pins['opencode']['platforms'][platform]['integrity']
    with Path(opencode).open('rb') as stream:
        digest = base64.b64encode(hashlib.file_digest(stream, 'sha512').digest()).decode()
    if 'sha512-' + digest != expected:
        raise ValueError('OpenCode archive checksum differs from the pinned distribution')
    with Path(muse).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != pins['muse']['platforms'][platform]['sha256']:
        raise ValueError('Muse binary checksum differs from the pinned distribution')


def qualify(opencode, muse, platform, image):
    pins = json.loads((ROOT / 'pins.json').read_text())
    verify_artifacts(opencode, muse, platform, pins)
    expected_arch = {'linux-arm64': 'arm64', 'linux-x64': 'amd64'}[platform]
    image_id, actual_arch = subprocess.check_output(['docker', 'image', 'inspect', image,
                  '--format', '{{.Id}} {{.Architecture}}'], text=True).strip().split()
    if actual_arch != expected_arch:
        raise ValueError('Runner image architecture does not match the pinned artifacts')
    name = 'forge-provider-spike-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='forge-provider-spike-') as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        with tarfile.open(opencode) as archive:
            # Extract exactly the verified executable, never arbitrary archive paths.
            with archive.extractfile('package/bin/opencode') as source:
                (directory / 'opencode').write_bytes(source.read())
        shutil.copyfile(muse, directory / 'muse')
        for binary in ('opencode', 'muse'):
            (directory / binary).chmod(0o755)
        for filename in ('offline.py', 'pins.json'):
            shutil.copyfile(ROOT / filename, directory / filename)
        command = ['docker', 'run', '--rm', '--name', name, '--network', 'none', '--read-only',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '1000:1000',
                   '--tmpfs', '/tmp:rw,exec,mode=1777', '--mount',
                   'type=bind,source=' + str(directory) + ',target=/qualification,readonly',
                   '--entrypoint', '/usr/local/bin/python3', image_id, '-I', '/qualification/offline.py']
        try:
            completed = subprocess.run(command, text=True, capture_output=True, timeout=180, check=True)
            return {**json.loads(completed.stdout), 'runner_image': image_id, 'platform': platform}
        finally:
            # A timed-out docker client does not stop its container. Reap this owned name.
            subprocess.run(['docker', 'rm', '--force', name], capture_output=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=['linux-arm64', 'linux-x64'])
    parser.add_argument('--opencode-archive', type=Path, required=True)
    parser.add_argument('--muse-binary', type=Path, required=True)
    parser.add_argument('--runner-image', required=True, help='Already installed Forge runner image')
    args = parser.parse_args()
    print(json.dumps(qualify(args.opencode_archive, args.muse_binary, args.platform, args.runner_image), indent=2))


if __name__ == '__main__':
    main()
