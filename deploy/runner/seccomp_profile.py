#!/usr/bin/env python3
"""Extend a checksum-verified Docker default profile for nested user namespaces.

No capabilities are added. Mount operations still need capability in the caller's
user namespace, and no host path/socket is exposed. All other upstream restrictions
remain in place. This prepares data only; installing AppArmor is a separate step.
"""
import argparse
import hashlib
import json
from pathlib import Path


def generate(path, expected):
    source = Path(path).read_bytes()
    if hashlib.sha256(source).hexdigest() != expected:
        raise ValueError('Docker default profile checksum mismatch')
    profile = json.loads(source)
    if profile.get('defaultAction') != 'SCMP_ACT_ERRNO':
        raise ValueError('Expected a default-deny Docker profile')
    profile['syscalls'].append({'names': ['unshare', 'mount', 'umount2', 'pivot_root', 'chroot', 'setns', 'clone'],
                                'action': 'SCMP_ACT_ALLOW', 'args': []})
    return profile


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source'); parser.add_argument('--sha256', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(generate(args.source, args.sha256), indent=2) + '\n')
