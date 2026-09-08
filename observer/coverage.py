"""Protected coverage provenance and durable per-commit scan receipts."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
from forge.notifications import atomic_json

POLICY = 'observer-v2'


def _key(repo):
    return hashlib.sha256(repo.repo.encode()).hexdigest()


def baseline_path(repo, state_dir):
    return Path(state_dir) / 'baselines' / (_key(repo) + '.json')


def _identity(repo):
    return {'repository': repo.repo, 'mirror': os.path.realpath(repo.observer_mirror)}


def declare_baseline(repo, sha, state_dir, reason):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha) or not reason.strip():
        raise ValueError('exact baseline SHA and reason required')
    value = {**_identity(repo), 'sha': sha, 'declared_at': time.time(), 'reason': reason,
             'coverage': 'commits after this SHA only', 'policy': POLICY}
    value['id'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    atomic_json(baseline_path(repo, state_dir), value)
    return value


def _read_baseline(repo, state_dir):
    path = baseline_path(repo, state_dir)
    if not path.is_file():
        raise ValueError('verified baseline required; legacy checkpoint is insufficient')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or any(value.get(k) != v for k, v in _identity(repo).items())
            or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value.get('sha', ''))
            or not isinstance(value.get('id'), str) or not value.get('reason')
            or value.get('policy') != POLICY):
        raise ValueError('baseline identity or policy mismatch')
    expected = hashlib.sha256(json.dumps({k: v for k, v in value.items() if k != 'id'}, sort_keys=True).encode()).hexdigest()
    if expected != value['id']:
        raise ValueError('baseline record is corrupt')
    return value


def receipt_path(repo, sha, state_dir):
    # A hash prevents untrusted identifiers ever becoming filesystem paths.
    return Path(state_dir) / 'receipts' / _key(repo) / (hashlib.sha256(sha.encode()).hexdigest() + '.json')


def verify_checkpoint(repo, sha, state_dir):
    baseline = _read_baseline(repo, state_dir)
    if sha == baseline['sha']:
        return
    path = receipt_path(repo, sha, state_dir)
    if not path.is_file():
        raise ValueError('checkpoint has no durable scan receipt')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('sha') != sha or value.get('baseline_id') != baseline['id']
            or value.get('policy') != POLICY or value.get('complete') is not True
            or value.get('level') not in ('benign', 'suspicious', 'malicious')
            or any(value.get(k) != v for k, v in _identity(repo).items())):
        raise ValueError('checkpoint receipt is incomplete or belongs to another baseline')


def record_scan(repo, commit, diff, verdict, state_dir):
    baseline = _read_baseline(repo, state_dir)
    value = {**_identity(repo), 'sha': commit.sha, 'author': commit.author,
             'subject': commit.subject, 'policy': POLICY, 'baseline_id': baseline['id'],
             'scanned_at': time.time(), 'patch_sha256': hashlib.sha256(diff.encode()).hexdigest(),
             'patch_characters': len(diff), 'complete': verdict.complete,
             'level': verdict.level, 'reason': verdict.reason,
             'confidence': verdict.confidence, 'findings': verdict.findings}
    atomic_json(receipt_path(repo, commit.sha, state_dir), value)
