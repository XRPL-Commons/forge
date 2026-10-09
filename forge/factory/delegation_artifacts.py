"""Exact, bounded Git output transferred between disposable child checkouts.

The controller never checks out or runs child code. It indexes the pack in an
empty object database and verifies its head and ancestry against assigned inputs.
Consumers verify the bundle again in their isolated checkout before merging it.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

MAX_BUNDLE_SIZE = 16 * 1024 * 1024
OID = re.compile(r'[a-f0-9]{40}(?:[a-f0-9]{24})?\Z')


def verify_bundle(content, head, inputs):
    if (not isinstance(content, bytes) or not 0 < len(content) <= MAX_BUNDLE_SIZE
            or not isinstance(head, str) or not OID.fullmatch(head)
            or not inputs or any(not isinstance(value, str) or not OID.fullmatch(value) for value in inputs)):
        raise ValueError('Invalid delegated output identity or size')
    header, separator, pack = content.partition(b'\n\n')
    if not separator or len(header) > 65536 or not pack.startswith(b'PACK'):
        raise ValueError('Invalid Git bundle')
    lines = header.decode('utf-8').splitlines()
    if not lines or lines.pop(0) not in ('# v2 git bundle', '# v3 git bundle'):
        raise ValueError('Unsupported Git bundle')
    prerequisites, tips = set(), []
    for line in lines:
        if line.startswith('@'):
            if line != '@object-format=sha256':
                raise ValueError('Unsupported Git bundle capability')
        elif line.startswith('-'):
            prerequisites.add(line[1:].split(' ', 1)[0])
        else:
            tips.append(line.split(' ', 1))
    if tips != [[head, 'HEAD']] or not prerequisites.issubset({inputs[0]}):
        raise ValueError('Delegated output differs from its pinned inputs')
    env = {**{key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_NO_REPLACE_OBJECTS': '1'}
    deadline = time.monotonic() + 15
    with tempfile.TemporaryDirectory(prefix='forge-bundle-') as directory:
        def git(*args, data=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('Delegated output verification exceeded its deadline')
            result = subprocess.run(['git', '-C', directory, *args], input=data, capture_output=True,
                                    timeout=remaining, env=env)
            if result.returncode:
                raise ValueError('Delegated Git output could not be verified')
            return result.stdout
        git('init', '--bare', '--quiet', '--object-format=' + ('sha256' if len(head) == 64 else 'sha1'))
        git('index-pack', '--stdin', '--max-input-size=' + str(MAX_BUNDLE_SIZE), data=pack)
        todo, seen, reached = [head], set(), set()
        while todo:
            oid = todo.pop()
            if oid in inputs:
                reached.add(oid)
            if oid in seen or oid == inputs[0]:
                continue
            seen.add(oid)
            if len(seen) > 10000:
                raise ValueError('Delegated output history exceeds the verification bound')
            if int(git('cat-file', '-s', oid)) > 65536:
                raise ValueError('Delegated commit metadata exceeds its bound')
            raw = git('cat-file', 'commit', oid)
            parents = [line[7:].decode('ascii') for line in raw.split(b'\n\n', 1)[0].splitlines() if line.startswith(b'parent ')]
            if not parents:
                raise ValueError('Delegated output does not descend from an assigned input')
            todo.extend(parents)
        if reached != set(inputs):
            raise ValueError('Delegated output omits an assigned prerequisite')
    return {'head_oid': head, 'digest': hashlib.sha256(content).hexdigest(), 'size': len(content)}
