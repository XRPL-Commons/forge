"""Operator-editable runtime policy, separate from credentials and GitHub discovery."""
import json
from pathlib import Path
import re

from .contracts import ROLES
from ..settings import read_json
from ..harnesses.registry import manifests

# The single source of runtime defaults: guided setup, examples and docs follow it
# (tests/test_runtime_examples.py). Slots are per-role ceilings; memory admission
# decides how many workers actually run, so ceilings are generous: a small host
# still runs only what fits, and a large one needs no tuning.
DEFAULT = {
    'version': 1, 'enabled': False, 'merge_enabled': False,
    'slots': {'build': 4, 'review': 2, 'audit': 1, 'recovery': 1},
    'cycle_seconds': 30,
    'timeout_seconds': {'build': 2700, 'review': 1200, 'audit': 1200, 'recovery': 600},
    'grace_seconds': 10, 'availability_retry_seconds': 300,
    'image': '', 'engine': '/usr/bin/docker', 'engine_socket': '/var/run/docker.sock',
    'apparmor_profile': 'forge-runner-private-proc', 'seccomp_profile_env': 'FORGE_RUNNER_SECCOMP',
    'network': 'forge-workers', 'proxy': 'http://forge-egress:3128',
    'memory': '4g', 'memory_tiers': [], 'host_reserve': '2g', 'cpus': 2, 'pids_limit': 256,
    'harness_argv': ['/usr/local/bin/python3', '-I', '/opt/forge/harness.py'], 'provider_environment': ['OPENAI_API_KEY'],
    'human_reviewers': [], 'human_required': False,
    'provider_auth_file_env': None, 'cycle_budget_seconds': 60,
    'provider': 'codex', 'model': None, 'provider_pool': [], 'provider_ready': None,
    'codex_parallel_auth': False,
    'ready_label': 'ready', 'hold_labels': ['hold', 'blocked'],
    'merge_method': 'squash',
}


def validate(value):
    if not isinstance(value, dict) or set(value) - (set(DEFAULT) | {'max_running'}):
        raise ValueError('Unknown runtime settings')
    # Older installations carry a manual total. Memory admission replaces it.
    config = {**DEFAULT, **{key: item for key, item in value.items() if key != 'max_running'}}
    if type(config['version']) is not int or config['version'] != 1:
        raise ValueError('Expected runtime version 1')
    if config['provider'] not in manifests():
        raise ValueError('Provider must name an installed harness')
    if config['model'] is not None and (not isinstance(config['model'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', config['model'])):
        raise ValueError('Invalid provider model')
    pool = config['provider_pool']
    if not isinstance(pool, list) or len(pool) > 20:
        raise ValueError('Configure at most twenty provider/model entries')
    ids, models = set(), set()
    for entry in pool:
        if not isinstance(entry, dict) or set(entry) != {'id', 'provider', 'model', 'provider_environment', 'provider_auth_file_env'}:
            raise ValueError('Each pool entry needs id, provider, model and credential references')
        if not isinstance(entry['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', entry['id']) or entry['id'] in ids:
            raise ValueError('Provider pool IDs must be unique')
        validate({**{k: v for k, v in entry.items() if k != 'id'}, 'provider_pool': []})
        identity = (entry['provider'], entry['model'])
        if identity in models:
            raise ValueError('Provider/model combinations must be unique; entries have equal weight')
        ids.add(entry['id']); models.add(identity)
    if config['provider_ready'] is not None and (not isinstance(config['provider_ready'], list)
            or any(not isinstance(v, str) or v not in ids for v in config['provider_ready'])):
        raise ValueError('Verified provider entries must belong to the configured pool')
    for key in ('enabled', 'merge_enabled', 'human_required', 'codex_parallel_auth'):
        if type(config[key]) is not bool:
            raise ValueError(key + ' must be boolean')
    for key, low, high in [('cycle_budget_seconds', 15, 300), ('cycle_seconds', 5, 3600), ('grace_seconds', 1, 60),
                            ('availability_retry_seconds', 30, 86400), ('cpus', 1, 64), ('pids_limit', 16, 4096)]:
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError('Invalid ' + key)
    for key, low, high in [('slots', 0, 64), ('timeout_seconds', 30, 3000)]:
        if not isinstance(config[key], dict) or set(config[key]) != set(ROLES):
            raise ValueError('Configure all four roles in ' + key)
        if any(type(v) is not int or not low <= v <= high for v in config[key].values()):
            raise ValueError('Invalid role limits in ' + key)
    if config['slots']['recovery'] < 1:
        raise ValueError('Mandatory investigations need at least one recovery slot')
    if config['image'] and not re.fullmatch(r'(?:[A-Za-z0-9._:/-]+@)?sha256:[0-9a-f]{64}', config['image']):
        raise ValueError('Pin the runner image by SHA256 digest or image ID')
    for key in ('engine', 'engine_socket'):
        if not isinstance(config[key], str) or not Path(config[key]).is_absolute():
            raise ValueError(key + ' must be an absolute path')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', config['network']):
        raise ValueError('Use a named internal worker network')
    if not re.fullmatch(r'http://[A-Za-z0-9-]+:[0-9]{1,5}', config['proxy']):
        raise ValueError('Use an internal HTTP CONNECT proxy name and port')
    if not re.fullmatch(r'[1-9][0-9]*[mg]', config['memory']):
        raise ValueError('memory must use m or g units')
    tiers = config['memory_tiers']
    if (not isinstance(tiers, list) or len(tiers) > 8
            or any(not isinstance(v, str) or not re.fullmatch(r'[1-9][0-9]*[mg]', v) for v in tiers)):
        raise ValueError('memory_tiers must be a list of at most eight memory limits')
    if tiers:
        from .capacity import memory_bytes
        sizes = [memory_bytes(v) for v in tiers]
        if tiers[0] != config['memory'] or sizes != sorted(set(sizes)):
            raise ValueError('memory_tiers must increase strictly from the default memory limit')
    if not re.fullmatch(r'[1-9][0-9]*[mg]', config['host_reserve']):
        raise ValueError('host_reserve must use m or g units')
    for key in ('harness_argv', 'provider_environment', 'human_reviewers', 'hold_labels'):
        if not isinstance(config[key], list) or any(not isinstance(v, str) or not v or '\x00' in v for v in config[key]):
            raise ValueError('Invalid ' + key)
    if config['apparmor_profile'] is not None and (not isinstance(config['apparmor_profile'], str) or not re.fullmatch(r'forge-[A-Za-z0-9_.-]+', config['apparmor_profile'])):
        raise ValueError('Use a dedicated Forge AppArmor profile name')
    if config['seccomp_profile_env'] is not None and (not isinstance(config['seccomp_profile_env'], str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', config['seccomp_profile_env'])):
        raise ValueError('Use a seccomp profile environment reference')
    if config['provider_auth_file_env'] is not None and (not isinstance(config['provider_auth_file_env'], str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', config['provider_auth_file_env'])):
        raise ValueError('Provider authentication file must use an environment reference')
    if any(not re.fullmatch(r'[A-Z][A-Z0-9_]+', v) or v in {'GH_TOKEN', 'GITHUB_TOKEN', 'HOME', 'PATH', 'LD_PRELOAD', 'PYTHONPATH'} for v in config['provider_environment']):
        raise ValueError('Provider references must be explicit environment variable names')
    if any(not re.fullmatch(r'[A-Za-z0-9-]+', v) for v in config['human_reviewers']):
        raise ValueError('Human reviewers must be GitHub logins, not bots')
    if config['merge_method'] not in ('merge', 'squash', 'rebase') or not isinstance(config['ready_label'], str) or not config['ready_label']:
        raise ValueError('Invalid merge method or ready label')
    if config['enabled'] and (not config['image'] or not config['harness_argv']):
        raise ValueError('Enabling requires a pinned image and harness command')
    return config


def load(root):
    path = Path(root) / 'runtime.json'
    return validate(read_json(path) if path.exists() else {})


def policy(config):
    return {key: config[key] for key in ('human_required', 'human_reviewers', 'merge_method')}
