"""Print narrowly scoped fixes; administrative GitHub credentials stay in gh."""
import json
import re
from urllib.parse import quote


def ruleset_command(repo, source, app_id):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or type(app_id) is not int or app_id <= 0:
        return ''
    identifier = source.get('id')
    if type(identifier) is not int or identifier <= 0 or source.get('bypass_actors_visible') is not True:
        return ''
    kind = source.get('source_type')
    path = f'repos/{repo}/rulesets/{identifier}' if kind == 'Repository' else f'orgs/{repo.split("/")[0]}/rulesets/{identifier}' if kind == 'Organization' else None
    if path is None:
        return ''
    expression = ('if (.bypass_actors | type) != "array" or (.rules | type) != "array" or .target != "branch" '
                  'then error("Full branch ruleset and bypass list required") else '
                  '{name,target,enforcement,conditions,rules,bypass_actors: '
                  '(.bypass_actors + (if any(.bypass_actors[]; .actor_type == "Integration" and .actor_id == '
                  + str(app_id) + ') then [] else [{actor_type:"Integration",actor_id:' + str(app_id)
                  + ',bypass_mode:"pull_request"}] end))} end')
    return f'forge_rules=$(gh api {path} | jq -e \'{expression}\') &&\nprintf \'%s\\n\' "$forge_rules" | gh api --method PUT {path} --input -'


def classic_command(repo, branch, slug, allowances):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not isinstance(slug, str) or not re.fullmatch(r'[A-Za-z0-9-]+', slug):
        return ''
    if not isinstance(allowances, dict) or any(not isinstance(allowances.get(key), list) for key in ('users', 'teams', 'apps')):
        return ''
    values = {key: list(allowances[key]) for key in ('users', 'teams', 'apps')}
    values['apps'] = list(dict.fromkeys([*values['apps'], slug]))
    body = json.dumps({'bypass_pull_request_allowances': values}, separators=(',', ':')).replace("'", "'\\''")
    return f"gh api -X PATCH repos/{repo}/branches/{quote(branch, safe='')}/protection/required_pull_request_reviews --input - <<< '{body}'"


def fixes(view):
    row = view.get('repository') or {}
    report = row.get('protection') or {}
    bypass = report.get('bypass') or {}
    repo = row.get('name', '')
    app_id = bypass.get('controller_app_id')
    slug = (view.get('connections', {}).get('controller') or {}).get('app_slug')
    app = slug or (f'ID {app_id}' if app_id else 'Forge controller')
    result = []
    if bypass.get('status') == 'missing':
        for source in bypass.get('sources', []):
            classic = source.get('kind') == 'classic'
            command = classic_command(repo, report.get('branch') or 'main', slug, source.get('bypass_allowances')) if classic else ruleset_command(repo, source, app_id)
            steps = [f'Open {source.get("url", "")}',
                     'Edit the branch rule, then allow specified actors to bypass required pull requests.' if classic else 'Open Bypass list and choose Add bypass.',
                     f'Add App {app}.' if classic else f'Add App {app} and select For pull requests only.', 'Save, then check again in Forge.']
            result.append({'id': str(source.get('id') or 'classic'), 'title': source.get('name') or 'Review rule',
                           'command': command or None, 'steps': steps, 'url': source.get('url')})
    for requirement in report.get('requirements', []):
        if not requirement.get('passed') and requirement.get('id') != 'readable':
            result.append({'id': requirement['id'], 'title': requirement['label'], 'command': None,
                           'steps': [requirement['detail'], 'Save, then check again in Forge.'], 'url': row.get('rules_url')})
    return {'repository': repo, 'fixes': result, 'read_only': True,
            'instruction': 'Run commands locally as the repository or organization administrator, then check again in Forge.'}
