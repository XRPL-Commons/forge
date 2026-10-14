"""Local effective-rule inspection and narrow, verified bypass repair."""
from copy import deepcopy
from urllib.parse import quote

from .contracts import SetupError, digest, positive, require


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


def ruleset_path(target, source_type, identifier):
    require(positive(identifier) and source_type in ('Repository', 'Organization'))
    return f"/repos/{target['name']}/rulesets/{identifier}" if source_type == 'Repository' else f"/orgs/{target['name'].split('/')[0]}/rulesets/{identifier}"


def snapshot(github, target, step):
    if step['kind'] == 'github.classic.repair_bypass':
        value = github._request('GET', classic_path(target) + '/required_pull_request_reviews')
        require(isinstance(value, dict), 'github_invalid_response')
        raw = value.get('bypass_pull_request_allowances') or {'users': [], 'teams': [], 'apps': []}
        result = {}
        for key, field in (('users', 'login'), ('teams', 'slug'), ('apps', 'slug')):
            require(isinstance(raw.get(key), list), 'github_invalid_response')
            result[key] = []
            for actor in raw[key]:
                require(isinstance(actor, dict) and isinstance(actor.get(field), str), 'github_invalid_response')
                result[key].append(actor[field])
        return {'bypass_pull_request_allowances': result}
    value = github._request('GET', ruleset_path(target, step['source_type'], step['ruleset_id']))
    require(isinstance(value, dict) and value.get('id') == step['ruleset_id'] and value.get('target') == 'branch'
            and isinstance(value.get('rules'), list) and isinstance(value.get('bypass_actors'), list), 'github_invalid_response')
    # GitHub can return extra read-only fields; never echo those into PUT.
    return {key: deepcopy(value.get(key)) for key in ('name', 'target', 'enforcement', 'conditions', 'rules', 'bypass_actors')}


def amended(value, app_id, slug=None):
    if 'bypass_pull_request_allowances' in value:
        require(isinstance(slug, str) and slug, 'controller_slug_required')
        result = deepcopy(value['bypass_pull_request_allowances'])
        if slug not in result['apps']:
            result['apps'].append(slug)
        return {'bypass_pull_request_allowances': result}
    result = deepcopy(value['bypass_actors'])
    if not any(actor.get('actor_type') == 'Integration' and actor.get('actor_id') == app_id for actor in result):
        result.append({'actor_id': app_id, 'actor_type': 'Integration', 'bypass_mode': 'pull_request'})
    return {'bypass_actors': result}


def app_slug(github, app_id):
    for page in range(1, 11):
        value = github._request('GET', f'/user/installations?per_page=100&page={page}')
        rows = value.get('installations') if isinstance(value, dict) else None
        require(isinstance(rows, list), 'github_invalid_response')
        for row in rows:
            if row.get('app_id') == app_id:
                require(isinstance(row.get('app_slug'), str) and row['app_slug'], 'controller_slug_required')
                return row['app_slug']
        if len(rows) < 100:
            break
    raise SetupError('controller_slug_required', 'Grant local access to the Controller installation before repairing classic protection.')


def inspect(github, target, app_id):
    """Only GET requests; inherited rules require their own administrative authority."""
    require(positive(app_id))
    effective = branch_rules(github, target)
    result = {'steps': [], 'blockers': [], 'signed_commits_required': any(row['type'] == 'required_signatures' for row in effective),
              'sources': [], 'workflow_permission': 'not_observed'}
    classic = optional(github, classic_path(target))
    if classic:
        require(isinstance(classic, dict), 'github_invalid_response')
        result['signed_commits_required'] |= bool((classic.get('required_signatures') or {}).get('enabled'))
        reviews = classic.get('required_pull_request_reviews')
        if reviews and reviews.get('required_approving_review_count', 0) > 0:
            step = {'kind': 'github.classic.repair_bypass'}
            step['app_slug'] = app_slug(github, app_id)
            value = snapshot(github, target, step)
            desired = amended(value, app_id, step['app_slug'])
            if desired != value:
                if target['admin']:
                    result['steps'].append({**step, 'before': digest(value), 'after': desired})
                else:
                    result['blockers'].append({'code': 'github_admin_required', 'source': 'classic'})
    seen = set()
    for rule in effective:
        if rule['type'] != 'pull_request' or (rule.get('parameters') or {}).get('required_approving_review_count', 0) <= 0:
            continue
        identifier, kind = rule.get('ruleset_id'), rule.get('ruleset_source_type')
        if (kind, identifier) in seen:
            continue
        seen.add((kind, identifier))
        result['sources'].append({'id': identifier, 'type': kind})
        if kind not in ('Repository', 'Organization'):
            result['blockers'].append({'code': 'ruleset_admin_required', 'ruleset_id': identifier})
            continue
        step = {'kind': 'github.ruleset.repair_bypass', 'ruleset_id': identifier, 'source_type': kind}
        value = snapshot(github, target, step)
        desired = amended(value, app_id)
        if desired['bypass_actors'] == value['bypass_actors']:
            continue
        if any(row.get('type') == 'required_signatures' for row in value['rules']):
            result['blockers'].append({'code': 'signing_bypass_refused', 'ruleset_id': identifier,
                                      'reason': 'Separate review bypass from required signatures on GitHub; Forge preserves signing rules.'})
            continue
        admin = target['admin']
        if kind == 'Organization':
            membership = optional(github, f"/user/memberships/orgs/{target['name'].split('/')[0]}") or {}
            admin = membership.get('state') == 'active' and membership.get('role') == 'admin'
        if not admin:
            result['blockers'].append({'code': 'organization_admin_required' if kind == 'Organization' else 'github_admin_required', 'ruleset_id': identifier})
            continue
        result['steps'].append({**step, 'before': digest(value), 'after': desired})
    return result


def confirmed(github, target, step):
    current = snapshot(github, target, step)
    return all(current.get(key) == value for key, value in step['after'].items())


def apply(github, target, step, app_id):
    current = snapshot(github, target, step)
    require(digest(current) == step['before'], 'plan_stale', 'The bypass list or branch rules changed; create a new plan.')
    expected = amended(current, app_id, step.get('app_slug'))
    require(expected == step['after'], 'plan_changed')
    if step['kind'] == 'github.ruleset.repair_bypass':
        require(not any(row.get('type') == 'required_signatures' for row in current['rules']), 'signing_bypass_refused')
        github._request('PUT', ruleset_path(target, step['source_type'], step['ruleset_id']), expected)
    else:
        github._request('PATCH', classic_path(target) + '/required_pull_request_reviews', expected)
    require(confirmed(github, target, step), 'github_write_unconfirmed', 'The bypass write has not been observed; inspect before resuming.')
