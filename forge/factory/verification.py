"""Commit-bound verification; workers cannot authorize a verifier or its result."""
from ..app_access import REPO_PATTERN


def configure(root, repository_id, checks, revision):
    """An explicit operator opt-in; repository content cannot change it."""
    from pathlib import Path
    from ..settings import Settings, digest, write_json
    from .config import load, validate, save
    settings = Settings(root)
    repo = next((r for r in settings.load()['repositories'] if str(r['id']) == repository_id), None)
    if not repo:
        raise ValueError('Use --target with a configured repository ID')
    if not isinstance(checks, list):
        raise ValueError('--checks must be a JSON list of check names and App IDs')
    with settings.locked('runtime-config'):
        current = load(root)
        if not revision or revision != digest(current):
            raise ValueError('Use the current runtime_revision from factory status as --revision')
        profiles = current['repository_verification']
        if checks:
            profiles[repo['name']] = checks
        else:
            profiles.pop(repo['name'], None)
        save(root, current, revision=revision)
    return {'ok': True, 'repository': repo['name'], 'checks': checks, 'runtime_revision': digest(current)}


def validate_profiles(profiles):
    if not isinstance(profiles, dict) or len(profiles) > 10000:
        raise ValueError('Repository verification must map repository names to allowed CI checks')
    for repo, checks in profiles.items():
        if (not isinstance(repo, str) or not REPO_PATTERN.fullmatch(repo)
                or not isinstance(checks, list) or not 1 <= len(checks) <= 20):
            raise ValueError('Configure one to twenty verification checks per repository')
        names = set()
        for check in checks:
            if (not isinstance(check, dict) or set(check) != {'name', 'app_id'}
                    or not isinstance(check['name'], str) or not check['name'].strip()
                    or len(check['name']) > 200 or check['name'].startswith('forge/')
                    or check['name'] in names or type(check['app_id']) is not int or check['app_id'] <= 0):
                raise ValueError('Each verification check needs a unique name and trusted GitHub App ID')
            names.add(check['name'])


def ci_result(snapshot, candidate, checks):
    if (not isinstance(snapshot, dict) or snapshot.get('head') != candidate['head']
            or snapshot.get('base') != candidate['base']):
        return False
    rows = snapshot.get('checks', {}).get(candidate['head'], [])
    for check in checks:
        matches = [r for r in rows if r.get('name') == check['name'] and r.get('app_id') == check['app_id']]
        if len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('conclusion') != 'success':
            return False
    return bool(checks)
