"""Repository evidence and automatic, bounded issue environment selection."""
import base64
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from .contracts import SHA, digest

SERVICES = ('mongodb', 'postgres', 'redis')
DEPENDENCIES = {'mongodb': {'mongodb', 'mongoose', 'mongodb-memory-server', 'mongodb-memory-server-core'},
                'postgres': {'pg', 'postgres', 'postgresql', 'psycopg', 'psycopg2', 'psycopg2-binary'},
                'redis': {'redis', 'ioredis', '@redis/client'}}


def services(value):
    if (not isinstance(value, list) or len(value) > len(SERVICES)
            or any(not isinstance(s, str) or s not in SERVICES for s in value)
            or len(set(value)) != len(value)):
        raise ValueError('Services must be a unique list of mongodb, postgres and redis')
    return sorted(value)


def validate_profiles(profiles):
    if not isinstance(profiles, dict):
        raise ValueError('Repository environments must be keyed by repository ID')
    for key, value in profiles.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]*', key)
                or not isinstance(value, dict) or set(value) - {'services', 'evidence', 'head', 'confirmed_at', 'default'}
                or not {'services', 'evidence', 'head', 'confirmed_at'} <= set(value)
                or ('default' in value and type(value['default']) is not bool)
                or not isinstance(value['head'], str) or not SHA.fullmatch(value['head'])
                or type(value['confirmed_at']) not in (int, float) or value['confirmed_at'] <= 0
                or not isinstance(value['evidence'], list) or len(value['evidence']) > 100
                or any(not isinstance(e, str) or len(e) > 1000 for e in value['evidence'])):
            raise ValueError('A repository environment needs a confirmed revision and evidence')
        services(value['services'])


def inspect_files(files):
    """Read dependency declarations, test setup and CI. Never execute repository code."""
    found, evidence = set(), []
    for path, content in sorted(files.items()):
        hits = set()
        if PurePosixPath(path).name == 'package.json':
            try:
                package = json.loads(content)
                names = set()
                for kind in ('dependencies', 'devDependencies', 'optionalDependencies'):
                    names.update(package.get(kind, {}))
                hits = {s for s, deps in DEPENDENCIES.items() if names & deps}
            except (ValueError, TypeError, AttributeError):
                continue
        else:
            patterns = {'mongodb': r'mongodb(?:-memory-server)?|mongoose|MONGODB_URI',
                        'postgres': r'postgres(?:ql)?|psycopg|\bpg\b',
                        'redis': r'\bredis\b|ioredis'}
            hits = {s for s, pattern in patterns.items() if re.search(pattern, content, re.I)}
        for service in sorted(hits):
            found.add(service)
            evidence.append(f'{path}: declares or references {service}')
    return {'services': sorted(found), 'evidence': evidence[:100]}


def probe(github, repo):
    branch = github.target(repo)
    commit = github.call('controller', repo, 'GET', '/commits/' + quote(branch, safe=''))
    head = commit.get('sha')
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError('Repository head could not be verified')
    tree = github.call('controller', repo, 'GET', f'/git/trees/{head}?recursive=1')
    if tree.get('truncated') or not isinstance(tree.get('tree'), list):
        raise ValueError('Repository tree is incomplete; inspect the environment manually')
    paths = []
    for row in tree['tree']:
        path = row.get('path', '')
        name = PurePosixPath(path).name
        if row.get('type') != 'blob' or row.get('mode') == '120000' or row.get('size', 0) > 65536:
            continue
        if any(part in {'node_modules', 'vendor', '.git', '.forge'} for part in PurePosixPath(path).parts):
            continue
        if (name == 'package.json' or name.startswith(('requirements', 'compose.', 'docker-compose.'))
                or name == 'pyproject.toml' or path.endswith('.prisma')
                or (path.startswith('.github/workflows/') and path.endswith(('.yml', '.yaml')))
                or (re.search(r'(?:test|spec|jest|vitest).*setup|setup.*(?:test|spec)', path, re.I)
                    and path.endswith(('.js', '.ts', '.mjs', '.cjs', '.py')))):
            paths.append(path)
    if len(paths) > 60:
        raise ValueError('Environment probe exceeds 60 files; inspect the environment manually')
    files = {}
    for path in sorted(paths):
        row = github.call('controller', repo, 'GET', '/contents/' + quote(path, safe='/') + '?ref=' + head)
        if row.get('encoding') != 'base64' or row.get('size', 0) > 65536:
            raise ValueError('Environment source could not be read completely')
        files[path] = base64.b64decode(row['content']).decode('utf-8')
    value = {'repo_id': repo['repo_id'], 'repo': repo['repo'], 'head': head, **inspect_files(files)}
    return {**value, 'revision': digest(value)}


def override(value):
    if not isinstance(value, dict) or set(value) != {'mode', 'services'} or value['mode'] not in ('automatic', 'standard', 'full'):
        raise ValueError('Choose automatic, standard, or database workers.')
    selected = services(value['services'])
    if (value['mode'] == 'full') != bool(selected):
        raise ValueError('Choose databases only for a database worker.')
    return {'mode': value['mode'], 'services': selected}


def choose(config, item, issue, evidence=None):
    """Choose per issue; repository scans are evidence, never an approval gate."""
    evidence = evidence or {}
    profile = config['repository_environments'].get(str(item['repo_id']), {})
    defaults = profile.get('services', []) if profile.get('default') else []
    known = evidence.get('services', profile.get('services', []))
    labels = {l['name'] if isinstance(l, dict) else l for l in issue.get('labels', [])}
    labels = {name for name in labels if name.startswith('env:')}
    if len(labels) > 1 or labels - {'env:standard', 'env:full'}:
        raise ValueError('Choose exactly one supported env:standard or env:full label')
    previous = item.get('environment_choice') or {}
    explicit = bool(labels and not (previous.get('automatic') and labels == {item.get('environment_label')}))
    manual = item.get('environment_override')
    title, body = issue.get('title', ''), issue.get('body') or ''
    text = title + '\n' + body
    mentioned = inspect_files({'issue-test-setup.txt': text})['services']
    docs = re.search(r'\b(?:copy|docs?|documentation)[ -]only\b|^(?:docs?|documentation|copy)\s*:', title, re.I)
    testing = re.search(r'\b(?:api|backend|server|database|integration|e2e|end.to.end|tests?|testing|migration)\b', text, re.I)
    automatic = not (manual or explicit)
    if manual:
        selected = manual['services']
        reason = 'Environment selected for this issue by an operator'
        tier = manual['mode']
    elif explicit:
        tier = next(iter(labels))[4:]
        selected = (known or defaults or list(SERVICES)) if tier == 'full' else []
        reason = 'Explicit issue environment label'
    elif item.get('environment_retry'):
        selected = item['environment_retry']['services']
        tier, reason = 'full', 'Worker reported missing databases; retrying this issue with them available'
    elif docs:
        tier, selected, reason = 'standard', [], 'This issue is scoped to documentation or copy'
    elif mentioned:
        tier, selected, reason = 'full', mentioned, 'The issue names these database dependencies'
    elif testing and known:
        tier, selected, reason = 'full', known, 'The issue concerns backend or test work; the repository scan found these databases'
    elif defaults:
        tier, selected, reason = 'full', defaults, 'Using the optional repository default for this issue'
    else:
        tier, selected, reason = 'standard', [], 'No database requirement identified for this issue; the worker can request one if needed'
    if tier == 'full' and 'full' not in config['environment_tiers']:
        raise ValueError('Database workers are unavailable on this host; enable the full environment.')
    return {'tier': tier, 'services': sorted(selected), 'reason': reason, 'automatic': automatic,
            'evidence_head': evidence.get('head'), 'evidence': evidence.get('evidence', []) if selected else []}


def worker_config(config, item):
    choice = item.get('environment_choice') or {'tier': 'standard', 'services': []}
    tier = choice['tier']
    limits = config['environment_tiers'].get(tier, {})
    return {**config, **limits, 'environment_tier': tier,
            'services': list(choice['services']) if tier == 'full' else []}
