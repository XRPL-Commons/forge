"""Read-only external GitHub rule observations; old mutations are retired."""
from urllib.parse import quote

from .contracts import SetupError, positive, require


def optional(github, path):
    try:
        return github._request('GET', path)
    except SetupError as error:
        if error.code == 'github_not_found':
            return None
        raise


def branch_rules(github, target):
    rules = []
    for page in range(1, 11):
        found = github._request('GET', f"/repos/{target['name']}/rules/branches/{quote(target['branch'], safe='')}?per_page=100&page={page}")
        require(isinstance(found, list) and all(isinstance(row, dict) and isinstance(row.get('type'), str) for row in found), 'github_invalid_response')
        rules.extend(found)
        if len(found) < 100:
            return rules
    raise SetupError('github_inventory_limit', 'Too many branch rules to inspect safely.')


def classic_path(target):
    return f"/repos/{target['name']}/branches/{quote(target['branch'], safe='')}/protection"


def inspect(github, target, app_id):
    """Observe external constraints without requesting administrative authority."""
    require(positive(app_id))
    effective = branch_rules(github, target)
    classic = optional(github, classic_path(target))
    require(classic is None or isinstance(classic, dict), 'github_invalid_response')
    sources = []
    for rule in effective:
        source = {'id': rule.get('ruleset_id'), 'type': rule.get('ruleset_source_type')}
        if source not in sources:
            sources.append(source)
    return {'steps': [], 'blockers': [],
            'signed_commits_required': any(row['type'] == 'required_signatures' for row in effective)
                or bool(((classic or {}).get('required_signatures') or {}).get('enabled')),
            'sources': sources, 'workflow_permission': 'not_observed'}


def apply(github, target, step, app_id):
    raise SetupError('github_protection_retired',
                     'Forge no longer changes GitHub protections or bypass actors. Existing rules remain in force.')
