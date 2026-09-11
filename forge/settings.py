"""Portable configuration, GitHub access observations, and explicit activation.

Configuration is operator intent; access.json is a replaceable observation. An
unknown/failed observation never silently removes configured repositories.
"""
import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .app_access import AppClient, AccessError, ROLE_PERMISSIONS, REPO_PATTERN, missing_permissions

ROLES = tuple(ROLE_PERMISSIONS)
DEFAULT = {'version': 1, 'sync_interval_seconds': 300, 'access_max_age_seconds': 900,
           'connections': {}, 'repositories': []}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate configuration key: ' + key)
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=_object)


def validate(config):
    if not isinstance(config, dict) or set(config) != set(DEFAULT) or type(config.get('version')) is not int or config['version'] != 1:
        raise ValueError('Expected version 1 settings with connections, repositories, and sync limits')
    for key in ('sync_interval_seconds', 'access_max_age_seconds'):
        if type(config[key]) is not int or not 30 <= config[key] <= 86400:
            raise ValueError(key + ' must be between 30 and 86400')
    if config['access_max_age_seconds'] < config['sync_interval_seconds']:
        raise ValueError('Access maximum age must be at least the sync interval')
    connections = config['connections']
    if not isinstance(connections, dict) or set(connections) - set(ROLES):
        raise ValueError('Connection roles must be build, review, audit, or controller')
    for role, value in connections.items():
        if not isinstance(value, dict) or set(value) not in ({'app_id', 'installation_id', 'private_key_env'}, {'app_id', 'installation_ids', 'private_key_env'}):
            raise ValueError(role + ': provide App ID, installation ID(s), and private key environment reference')
        ids = value.get('installation_ids', [value.get('installation_id')])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20:
            raise ValueError(role + ': provide one to twenty installation IDs')
        if any(type(v) is not int or not 0 < v < 2**53 for v in [value['app_id'], *ids]) or len(set(ids)) != len(ids):
            raise ValueError(role + ': App and installation IDs must be distinct positive integers')
        if not isinstance(value['private_key_env'], str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', value['private_key_env']):
            raise ValueError(role + ': private_key_env must be an environment variable name, not a secret')
    if len({c['app_id'] for c in connections.values()}) != len(connections):
        raise ValueError('Use a distinct App identity for each role')
    if not isinstance(config['repositories'], list) or len(config['repositories']) > 10000:
        raise ValueError('repositories must be a bounded list')
    ids, names = set(), set()
    for row in config['repositories']:
        if (not isinstance(row, dict) or set(row) != {'id', 'name', 'enabled'}
                or type(row['id']) is not int or row['id'] <= 0
                or type(row['enabled']) is not bool or not isinstance(row['name'], str)
                or not REPO_PATTERN.fullmatch(row['name'])):
            raise ValueError('Each repository needs a GitHub numeric id, owner/name, and enabled boolean')
        if row['id'] in ids or row['name'].lower() in names:
            raise ValueError('Duplicate repository configuration')
        ids.add(row['id']); names.add(row['name'].lower())
    return config


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Settings:
    def __init__(self, root, *, client=None, clock=time.time):
        self.root = Path(root)
        self.path = self.root / 'factory.json'
        self.state = self.root / 'state' / 'github-access'
        self.client = client or AppClient()
        self.clock = clock

    def load(self):
        return validate(read_json(self.path)) if self.path.exists() else json.loads(json.dumps(DEFAULT))

    @contextmanager
    def locked(self, name, *, blocking=True):
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / (name + '.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield

    def observation(self):
        try:
            value = read_json(self.state / 'access.json')
            return value if isinstance(value, dict) and isinstance(value.get('roles'), dict) else {'roles': {}}
        except (OSError, ValueError):
            return {'roles': {}}

    def save(self, config, revision):
        validate(config)
        with self.locked('config'):
            if revision != digest(self.load()):
                raise ValueError('Settings changed; reload before saving')
            # Access checks use the NEW connection configuration. Replacing an
            # App cannot inherit the previous App's successful observation.
            view = self.view(config=config)
            rows = {r['id']: r for r in view['repositories']}
            old = {r['id']: r for r in self.load()['repositories']}
            for row in config['repositories']:
                previous = old.get(row['id'], {})
                newly_enabled = row['enabled'] and (not previous.get('enabled') or previous.get('name') != row['name'])
                if newly_enabled and not rows[row['id']]['access_ready']:
                    raise ValueError(row['name'] + ': verify access for every role before enabling')
            write_json(self.path, config)
        return self.view()

    def refresh(self):
        try:
            with self.locked('sync', blocking=False):
                config, previous = self.load(), self.observation()
                start = self.clock()
                write_json(self.state / 'sync.json', {'status': 'running', 'started_at': start})
                def collect(role):
                    connection = config['connections'].get(role)
                    if not connection:
                        return role, {'status': 'not_configured', 'observed_at': self.clock()}
                    row = {'connection': digest(connection), 'observed_at': self.clock()}
                    try:
                        row.update(self.client.discover(connection), status='ok')
                    except Exception as error:
                        reason = str(error) if isinstance(error, AccessError) else 'Access sync failed; retry or inspect server configuration'
                        old = previous['roles'].get(role, {})
                        # Retain last known inventory ONLY for the same identity.
                        if old.get('connection') == row['connection']:
                            row.update({k: old[k] for k in ('repositories', 'app_slug', 'account', 'installation_url', 'permissions') if k in old})
                        row.update(status='unknown', reason=reason)
                    row['observed_at'] = self.clock()
                    return role, row
                with ThreadPoolExecutor(max_workers=4) as pool:
                    roles = dict(pool.map(collect, ROLES))
                write_json(self.state / 'access.json', {'roles': roles, 'observed_at': self.clock()})
                write_json(self.state / 'sync.json', {'status': 'complete', 'finished_at': self.clock()})
                return True
        except BlockingIOError:
            return False

    def view(self, *, config=None):
        config = self.load() if config is None else validate(config)
        observation, now = self.observation(), self.clock()
        role_views, inventories = {}, {}
        candidates = {r['id']: dict(r) for r in config['repositories']}
        for role in ROLES:
            connection = config['connections'].get(role)
            row = observation['roles'].get(role, {})
            matches = bool(connection and row.get('connection') == digest(connection))
            age = now - row.get('observed_at', 0) if type(row.get('observed_at')) in (int, float) else float('inf')
            status = row.get('status', 'unknown') if matches else ('unknown' if connection else 'not_configured')
            if status == 'ok' and not 0 <= age <= config['access_max_age_seconds']:
                status = 'stale'
            inventory = row.get('repositories', []) if matches else []
            inventories[role] = {r['id']: r for r in inventory}
            for repo in inventory:
                candidates.setdefault(repo['id'], {**repo, 'enabled': False})
            missing = missing_permissions(role, row.get('permissions', {})) if matches else []
            if status == 'ok' and missing:
                status = 'missing_permissions'
            role_views[role] = {'status': status, 'missing_permissions': missing,
                                'required_permissions': ROLE_PERMISSIONS[role],
                                'reason': row.get('reason') if matches else None,
                                'observed_at': row.get('observed_at') if matches else None,
                                'installation_url': row.get('installation_url') if matches else None,
                                'app_slug': row.get('app_slug') if matches else None}
        repositories = []
        configured = {r['id'] for r in config['repositories']}
        for identifier, repo in candidates.items():
            access = {}
            for role in ROLES:
                status = role_views[role]['status']
                observed = inventories[role].get(identifier)
                if status == 'ok':
                    status = ('missing_access' if not observed else
                              'name_changed' if observed['name'].lower() != repo['name'].lower() else
                              'archived' if observed.get('archived') or observed.get('disabled') else 'ready')
                access[role] = status
            ready = all(s == 'ready' for s in access.values())
            repositories.append({'id': identifier, 'name': repo['name'], 'enabled': repo['enabled'],
                                 'configured': identifier in configured, 'access': access,
                                 'access_ready': ready, 'dispatch_allowed': repo['enabled'] and ready})
        try:
            sync = read_json(self.state / 'sync.json')
        except (OSError, ValueError):
            sync = {'status': 'never'}
        # A server crash must not leave the UI permanently showing "refreshing".
        try:
            with self.locked('sync', blocking=False):
                if sync.get('status') == 'running':
                    sync = {**sync, 'status': 'interrupted'}
        except BlockingIOError:
            pass
        return {'config': config, 'revision': digest(config), 'configured': self.path.exists(),
                'roles': role_views, 'repositories': sorted(repositories, key=lambda r: r['name'].lower()),
                'sync': sync, 'config_file': 'factory.json'}

    def admission(self, repo):
        if not self.path.exists():
            return {'allowed': True, 'reason': 'legacy_configuration'}
        try:
            row = next((r for r in self.view()['repositories'] if r['name'].lower() == repo.lower()), None)
            if not row or not row['enabled']:
                return {'allowed': False, 'reason': 'repository_not_enabled'}
            return {'allowed': row['dispatch_allowed'], 'reason': 'access_verified' if row['dispatch_allowed'] else 'github_access_unverified',
                    'roles': row['access']}
        except (OSError, ValueError, TypeError, KeyError):
            return {'allowed': False, 'reason': 'settings_unavailable'}

    def start_refresh(self):
        thread = threading.Thread(target=self.refresh, daemon=True)
        thread.start()

    def watch(self):
        while True:
            try:
                if self.path.exists():
                    self.refresh()
                interval = self.load()['sync_interval_seconds']
            except (OSError, ValueError):
                interval = 300
            time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['show', 'sync', 'validate', 'watch'])
    parser.add_argument('--root', required=True, help='Directory containing factory.json and state/')
    args = parser.parse_args(argv)
    settings = Settings(args.root)
    try:
        if args.command == 'watch':
            settings.watch()
            return
        if args.command == 'sync':
            if not settings.path.exists():
                raise ValueError('factory.json does not exist')
            started = settings.refresh()
            result = settings.view()
            print(json.dumps(result, indent=2))
            if not started:
                return 2
            return 0 if result['config']['connections'] and all(
                result['roles'][role]['status'] == 'ok' for role in result['config']['connections']) else 1
        if args.command == 'validate':
            if not settings.path.exists():
                raise ValueError('factory.json does not exist')
            settings.load()
            print('Configuration is valid')
        else:
            print(json.dumps(settings.view(), indent=2))
    except (ValueError, OSError) as error:
        parser.exit(1, f'Settings unavailable: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
