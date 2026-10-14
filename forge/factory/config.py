"""Operator-editable runtime policy, separate from credentials and GitHub discovery."""
from copy import deepcopy
from pathlib import Path
import re

from .contracts import ROLES
from ..harnesses.registry import manifests

# The single source of runtime defaults: guided setup, examples and docs follow it
# (tests/test_runtime_examples.py). Slots are per-role ceilings; memory admission
# decides how many workers actually run, so ceilings are generous: a small host
# still runs only what fits, and a large one needs no tuning.
DEFAULT = {
    'version': 1, 'enabled': False, 'merge_enabled': False,
    'slots': {'build': 4, 'review': 2, 'audit': 1, 'recovery': 1, 'steward': 1},
    'cycle_seconds': 30,
    'timeout_seconds': {'build': 2700, 'review': 1200, 'audit': 1200, 'recovery': 600, 'steward': 600},
    'grace_seconds': 10, 'availability_retry_seconds': 300,
    'reliability': {'retry_seconds': 30, 'stall_seconds': 300,
                    'effect_retry_cap_seconds': 900, 'effect_max_attempts': 8,
                    'effect_uncertainty_seconds': 900, 'claim_retest_seconds': [300, 1800, 7200]},
    'image': '', 'engine': '/usr/bin/docker', 'engine_socket': '/var/run/docker.sock',
    'apparmor_profile': 'forge-runner-private-proc', 'seccomp_profile_env': 'FORGE_RUNNER_SECCOMP',
    'network': 'forge-workers', 'proxy': 'http://forge-egress:3128',
    # pids_limit counts threads too: a dev server, pnpm and a headless Chromium
    # together exceed 256 (pthread_create: Resource temporarily unavailable).
    'memory': '4g', 'memory_tiers': [], 'host_reserve': '2g', 'cpus': 2, 'pids_limit': 1024,
    'environment_tiers': {}, 'repository_environments': {},
    'repository_verification': {}, 'repository_merge_policies': {},
    'harness_argv': ['/usr/local/bin/python3', '-I', '/opt/forge/harness.py'], 'provider_environment': ['OPENAI_API_KEY'],
    'human_reviewers': [], 'human_required': False,
    'provider_auth_file_env': None, 'cycle_budget_seconds': 60,
    'provider': 'codex', 'model': None, 'provider_pool': [], 'provider_ready': None,
    # Issue conversations only: a faster model per provider, e.g. {"claude": "claude-haiku-4-5"}.
    # Null keeps each pool entry's own model. Pipeline roles never use it.
    'discussion_model': None,
    'codex_parallel_auth': False,
    'ready_label': 'ready', 'hold_labels': ['hold', 'blocked'],
    'merge_method': 'squash',
}

MODEL = r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}'


def validate(value):
    return _validate(value, manifests())


def _validate(value, installed):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('Unknown runtime settings')
    config = deepcopy({**DEFAULT, **value})
    if type(config['version']) is not int or config['version'] != 1:
        raise ValueError('Expected runtime version 1')
    if config['provider'] not in installed:
        raise ValueError('Provider must name an installed harness')
    if config['model'] is not None and (not isinstance(config['model'], str) or not re.fullmatch(MODEL, config['model'])):
        raise ValueError('Invalid provider model')
    quick = config['discussion_model']
    if quick is not None and (not isinstance(quick, dict) or len(quick) > 20
            or any(provider not in installed for provider in quick)
            or any(not isinstance(v, str) or not re.fullmatch(MODEL, v) for v in quick.values())):
        raise ValueError('discussion_model maps installed providers to model names')
    pool = config['provider_pool']
    if not isinstance(pool, list) or len(pool) > 20:
        raise ValueError('Configure at most twenty provider/model entries')
    ids, models = set(), set()
    for entry in pool:
        if not isinstance(entry, dict) or set(entry) != {'id', 'provider', 'model', 'provider_environment', 'provider_auth_file_env'}:
            raise ValueError('Each pool entry needs id, provider, model and credential references')
        if not isinstance(entry['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', entry['id']) or entry['id'] in ids:
            raise ValueError('Provider pool IDs must be unique')
        _validate({**{k: v for k, v in entry.items() if k != 'id'}, 'provider_pool': []}, installed)
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
        # Immutable pre-steward launch snapshots still need verified teardown.
        if isinstance(config[key],dict) and set(config[key]) == set(ROLES)-{'steward'}:
            config[key] = {**config[key], 'steward':config[key]['recovery']}
        if not isinstance(config[key], dict) or set(config[key]) != set(ROLES):
            raise ValueError('Configure all roles in ' + key)
        if any(type(v) is not int or not low <= v <= high for v in config[key].values()):
            raise ValueError('Invalid role limits in ' + key)
    if config['slots']['recovery'] < 1:
        raise ValueError('Mandatory investigations need at least one recovery slot')
    if config['slots']['steward'] < 1:
        raise ValueError('Steward decisions need at least one steward slot')
    reliability = config['reliability']
    if not isinstance(reliability, dict) or set(reliability) - set(DEFAULT['reliability']):
        raise ValueError('Unknown reliability settings')
    reliability = config['reliability'] = {**deepcopy(DEFAULT['reliability']), **reliability}
    for name, low, high in [('retry_seconds', 1, 3600), ('stall_seconds', 30, 86400),
                          ('effect_retry_cap_seconds', 1, 86400), ('effect_max_attempts', 1, 100),
                          ('effect_uncertainty_seconds', 30, 86400)]:
        if type(reliability[name]) is not int or not low <= reliability[name] <= high:
            raise ValueError('Invalid reliability ' + name)
    if reliability['effect_retry_cap_seconds'] < reliability['retry_seconds']:
        raise ValueError('Effect retry cap must be at least the initial delay')
    delays = reliability['claim_retest_seconds']
    if (not isinstance(delays, list) or not 1 <= len(delays) <= 8
            or any(type(delay) is not int or not 30 <= delay <= 7200 for delay in delays)
            or delays != sorted(set(delays))):
        raise ValueError('Claim re-test delays must increase, between 30 and 7200 seconds')
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
    tiers = config['environment_tiers']
    if not isinstance(tiers, dict) or set(tiers) - {'standard', 'full'}:
        raise ValueError('Environment tiers must be standard or full')
    for tier in tiers.values():
        if (not isinstance(tier, dict) or set(tier) != {'image', 'memory', 'cpus'}
                or not isinstance(tier['image'], str) or not tier['image']
                or not isinstance(tier['memory'], str)):
            raise ValueError('Each environment tier needs a pinned image, memory and cpus')
        _validate({**tier, 'environment_tiers': {}, 'repository_environments': {}}, installed)
    from .environments import validate_profiles
    validate_profiles(config['repository_environments'])
    policies = config['repository_merge_policies']
    if (not isinstance(policies,dict) or len(policies)>10000
            or any(not isinstance(key,str) or not re.fullmatch(r'[1-9][0-9]*',key)
                   or int(key)>=2**53 or not isinstance(mode,str) or mode not in {'auto','human'} for key,mode in policies.items())):
        raise ValueError('Repository merge policies map immutable GitHub IDs to auto or human')
    from .verification import validate_profiles as validate_verification
    validate_verification(config['repository_verification'])
    return config


def load(root, *, scope=None, host_default=False):
    from ..configuration_authority import ConfigurationAuthority
    row = ConfigurationAuthority(root, scope=scope).runtime(host_default=host_default)
    return validate(row['settings'] if row else {})


def save(root, config, *, revision=None, scope=None, storage_revision=None):
    from ..settings import digest
    from ..configuration_authority import ConfigurationAuthority
    from ..store.configuration import ConfigurationConflict
    config = validate(config)
    authority = ConfigurationAuthority(root, scope=scope)
    domain = authority.domain('runtime.json')
    previous = authority.snapshot(domain)
    current = validate(previous['settings'] if previous else {})
    if revision is not None and revision != digest(current):
        raise ConfigurationConflict('Runtime settings changed; reload before saving')
    authority.replace(domain, config, expected_revision=(storage_revision if storage_revision is not None
                                                         else previous['revision'] if previous else 0),
                      credential_ref=previous['credential_ref'] if previous else None)
    return config


def legacy_merge_policies(config, repositories):
    """Freeze an already enabled legacy auto policy to currently configured IDs."""
    mode = 'auto' if config.get('merge_enabled') is True and config.get('human_required') is False else 'human'
    policies = {str(repo['id']):mode for repo in repositories}
    policies.update(config.get('repository_merge_policies',{}))
    return {**config,'repository_merge_policies':policies}


def merge_mode(config, repository_id):
    if type(repository_id) is not int or not 0<repository_id<2**53:
        raise ValueError('Merge policy requires an immutable GitHub repository ID')
    return config.get('repository_merge_policies',{}).get(str(repository_id),'human')


def policy(config, repository_id=None, *, repository_name=None):
    if repository_id is None:
        # The retired JSON supervisor has no per-repository policy contract.
        value = {key: config[key] for key in ('human_required', 'human_reviewers', 'merge_method')}
        if config.get('repository_verification'):
            value['repository_verification'] = config['repository_verification']
        return value
    mode = merge_mode(config,repository_id)
    return dict(repository_id=repository_id,merge_policy=mode,human_required=mode=='human',
                human_reviewers=config['human_reviewers'],merge_method=config['merge_method'],
                verification_checks=config.get('repository_verification',{}).get(repository_name,[]))
