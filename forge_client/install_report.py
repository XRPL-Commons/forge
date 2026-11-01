"""Private handover evidence, projected without credentials or raw diagnostics."""
from urllib.parse import urlsplit
import shlex
import math

from .workflow import Journal
from .contracts import digest


def pick(value, *keys):
    return {key: value[key] for key in keys if key in value}


def receipt(state, stage):
    return state.get('stages', {}).get(stage, {}).get('receipt') or {}


def rule_changes(value):
    return [pick(row, 'kind', 'ruleset_id', 'source_type', 'before')
            for row in value.get('completed', []) if isinstance(row, dict)
            and row.get('kind') in ('github.ruleset.ensure_baseline', 'github.ruleset.repair_bypass',
                                   'github.ruleset.repair_baseline', 'github.classic.repair_bypass')]


def remember_rules(stage, value):
    """Later readiness observations must not erase confirmed preparation writes."""
    if 'completed' in value:
        stage.setdefault('rule_changes', [])
    for change in rule_changes(value):
        saved = stage.setdefault('rule_changes', [])
        if change not in saved:
            saved.append(change)


def storage(journal, state):
    run = digest({'inputs_digest': state['inputs_digest'], 'started_at': state['started_at']})
    return Journal('installation-report', run, root=journal.root)


def smoke_receipt(value):
    # Keep exact proof identifiers and timings, never worker output or arbitrary
    # future receipt fields. The original receipt remains in the private journal.
    result = pick(value, 'status', 'number', 'started_at', 'merged_at', 'elapsed_seconds',
                  'verified_at', 'setup_id', 'generation', 'intent_digest')
    result['repository'] = pick(value.get('repository') or {}, 'id', 'name')
    result['candidate'] = pick(value.get('candidate') or {}, 'id', 'pull_number', 'head_sha', 'base_sha', 'merge_sha')
    evidence = value.get('evidence') or {}
    result['evidence'] = pick(evidence, 'merge_effect_id', 'confirmed_at', 'approval_hold_observed')
    result['evidence']['role_jobs'] = pick(evidence.get('role_jobs') or {}, 'build', 'review', 'audit')
    result['attempts'] = pick(value.get('attempts') or {}, 'total', 'charged_failures')
    result['triage'] = pick(value.get('triage') or {}, 'id', 'execution_id', 'row_id', 'action', 'unit_id')
    result['repositories'] = [dict(target=pick(row['target'], 'id', 'name'),
                                   **pick(row, 'merge_policy', 'allow_workflow_edits'))
                              for row in value.get('repositories', [])]
    return result


def origin(value):
    parsed = urlsplit(value or '')
    if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
        return 'https://' + parsed.netloc
    return None


def preparation_evidence(state):
    """Project the server receipt only for this approved installation plan."""
    plan = (state.get('server_preparation') or {}).get('plan')
    value = receipt(state, 'preparation')
    if not plan or value.get('available') is not True:
        return {}, {}
    expected = {'intent_digest': state['inputs_digest'], 'started_at': state['started_at'],
                'repositories': plan['repositories'], 'smoke_repository': plan['smoke_repository']}
    if not all(value.get(key) == part for key, part in expected.items()):
        return {}, {}
    handover = receipt(state, 'handover')
    if handover.get('status') == 'handover_complete' and not all(
            value.get(key) == handover.get(key) for key in ('setup_id', 'generation', 'intent_digest', 'started_at', 'repositories')):
        return {}, {}
    targets = {row['target']['id'] for row in plan['repositories']} | {plan['smoke_repository']['id']}
    allowed = {'environment', 'settings', 'access', 'smoke.prepare'} | {
        prefix + str(identifier) for identifier in targets for prefix in ('repository.', 'publication.')} | {
        'account.' + row['id'] for row in plan['providers']}
    stages = {}
    for row in value.get('stages', []):
        if row.get('id') not in allowed or row.get('status') not in ('pending', 'running', 'waiting', 'complete', 'needs_operator', 'failed'):
            continue
        stage = {'status': row['status']}
        for key in ('started_at', 'completed_at'):
            timestamp = row.get(key)
            if type(timestamp) in (int, float) and math.isfinite(timestamp) and timestamp > 0:
                stage[key] = timestamp
        stages[row['id']] = stage
    providers = {(row['id'], row['kind']): row['connected'] for row in value.get('providers', [])
                 if type(row.get('connected')) is bool and {'id': row.get('id'), 'kind': row.get('kind')} in plan['providers']}
    return stages, providers


def build(state, *, versions=None):
    """Use only recorded evidence; completion does not establish a clean benchmark."""
    intent, local_stages = state['intent'], state.get('stages', {})
    server_stages, server_providers = preparation_evidence(state)
    stages = {**local_stages, **server_stages}
    preflight = (state.get('preflight') or {}).get('report') or {}
    observations = {row['target']['id']: row for row in preflight.get('repositories', [])}
    handover = receipt(state, 'handover')
    smoke = smoke_receipt(handover if handover.get('status') == 'handover_complete' else receipt(state, 'smoke'))
    versions = versions or {'client_revision': None, 'server_revision': None, 'observed_at': None}
    recorded_versions = {**pick(versions, 'client_revision', 'server_revision', 'observed_at'),
                         'requested_revision': intent.get('revision'),
                         'host_install_requested_revision': receipt(state, 'host').get('revision'),
                         'harness_versions': None, 'worker_image': None,
                         'qualified_environment_image': receipt(state, 'environment').get('image')}
    missing = ['Worker image and harness versions are not recorded in the installation receipts.',
               'Human touch count, early consent and clean-install benchmark were not measured by this report.']
    if not recorded_versions.get('client_revision'):
        missing.append('The reporting client revision is unknown.')
    if not recorded_versions.get('server_revision'):
        missing.append('The current server revision could not be observed; the requested revision is not a substitute.')
    app = receipt(state, 'consent')
    install_url = urlsplit(app.get('install_url') or '')
    parts = install_url.path.strip('/').split('/')
    slug = parts[1] if install_url.hostname == 'github.com' and len(parts) == 4 and parts[0] == 'apps' and parts[2:] == ['installations', 'new'] else None
    apps = {'app_id': app.get('app_id'), 'slug': slug, 'installation_id': app.get('installation_id'),
            'status': app.get('status', 'unknown')}
    if not apps['installation_id']:
        missing.append('The App installation ID is not recorded in the consent receipt.')
    repositories = []
    for selected in intent['repositories']:
        target = selected['target']; name = 'repository.' + str(target['id'])
        stage, observed = stages.get(name, {}), observations.get(target['id'], {})
        local = local_stages.get(name, {})
        changes = local.get('rule_changes', rule_changes(local.get('receipt') or {}))
        repositories.append({'repository': pick(target, 'id', 'name'),
            'merge_policy': selected['merge_policy'], 'github_protection': selected.get('github_protection', 'required'), 'allow_workflow_edits': selected['allow_workflow_edits'],
            'stage_status': stage.get('status', 'unknown'), 'environment_tier': observed.get('env_tier'),
            'environment_observed_at': (state.get('preflight') or {}).get('observed_at'),
            'rules': {'signed_commits_required': observed.get('signed_commits_required'),
                      'classic_protection': observed.get('classic_protection'),
                      'sources': [pick(row, 'type', 'id', 'editable', 'enforcement') for row in observed.get('rule_sources', [])],
                      'confirmed_changes': changes,
                      'change_history': 'recorded' if 'rule_changes' in local or 'completed' in (local.get('receipt') or {}) else 'unknown'},
            'publication_status': ('qualified' if server_stages['publication.' + str(target['id'])]['status'] == 'complete'
                                   else server_stages['publication.' + str(target['id'])]['status'])
                                  if 'publication.' + str(target['id']) in server_stages
                                  else receipt(state, 'publication.' + str(target['id'])).get('status', 'unknown')})
    providers = [{'id': row['id'], 'kind': row['kind'],
                  'status': ('connected' if server_providers[(row['id'], row['kind'])] else 'not_connected')
                            if (row['id'], row['kind']) in server_providers
                            else receipt(state, 'account.' + row['id']).get('status', 'unknown')}
                 for row in intent['providers']]
    timings = []
    for name, stage in stages.items():
        start, end = stage.get('started_at'), stage.get('completed_at')
        timings.append({'stage': name, 'status': stage['status'], 'started_at': start, 'completed_at': end,
                        'elapsed_seconds': max(0, end-start) if start is not None and end is not None else None})
    # Do not copy free-form errors, browser/device URLs, codes or arbitrary command
    # arguments from pending receipts into the report.
    remaining = [{'stage': name, 'status': stage['status'],
                  'action': 'Review this step in the Forge setup page.' if name in server_stages else
                            'Inspect this stage in forge install --status, then resume the saved installation.'}
                 for name, stage in stages.items() if stage['status'] != 'complete']
    waived = [{'id': row['id'], 'status': 'waived'} for row in preflight.get('items', [])
              if row['id'] in state.get('waivers', [])]
    address = origin(receipt(state, 'identity').get('origin'))
    candidate = smoke['candidate']; repository = smoke['repository']
    pull_url = ('https://github.com/' + repository['name'] + '/pull/' + str(candidate['pull_number'])
                if repository.get('name') and candidate.get('pull_number') else None)
    return {'version': 1, 'status': 'handover_complete' if handover.get('status') == 'handover_complete' else 'incomplete',
            'inputs_digest': state['inputs_digest'], 'started_at': state.get('started_at'),
            'completed_at': state.get('completed_at'), 'versions': recorded_versions,
            'apps': apps, 'repositories': repositories, 'providers': providers, 'stages': timings,
            'smoke': smoke, 'smoke_pull_url': pull_url,
            'time_to_first_merge_seconds': smoke.get('elapsed_seconds'),
            'benchmark': {'clean_install': 'not_established', 'human_touches': None},
            'remaining_actions': remaining, 'waived_findings': waived, 'missing_evidence': missing,
            'origin': address, 'connect_command': ['forge', 'connect', address] if address else None}


def display(report, *, tell=print):
    versions = report['versions']
    tell('Handover summary')
    tell('  Client: ' + (versions['client_revision'] or 'unknown'))
    tell('  Server: ' + (versions['server_revision'] or 'unknown') + ' (observed after handover)')
    tell('  Requested revision: ' + (versions['requested_revision'] or 'unknown'))
    app = report['apps']
    tell('  GitHub App: ' + str(app['app_id'] or 'unknown') + '; installation: ' + str(app['installation_id'] or 'unknown'))
    for row in report['repositories']:
        tell('  ' + row['repository']['name'] + ': ' + row['merge_policy'] + ' merge; environment '
             + (row['environment_tier'] or 'unknown') + '; ' + row['stage_status'])
    for row in report['providers']:
        tell('  ' + row['id'] + ': ' + row['status'])
    if report['smoke_pull_url']:
        tell('  Smoke merge: ' + report['smoke_pull_url'])
    if report['time_to_first_merge_seconds'] is not None:
        tell('  Time to first merge: ' + str(round(report['time_to_first_merge_seconds'], 1)) + ' seconds (includes interruptions)')
    for row in report['stages']:
        if row['elapsed_seconds'] is not None:
            tell('  ' + row['stage'] + ': ' + str(round(row['elapsed_seconds'], 1)) + ' seconds')
    for row in report['remaining_actions']:
        tell('  Remaining: ' + row['stage'] + ' — ' + row['action'])
    for row in report['waived_findings']:
        tell('  Waived prerequisite: ' + row['id'])
    for gap in report['missing_evidence']:
        tell('  Evidence: ' + gap)
    if report['origin']:
        tell('Forge: ' + report['origin'])
    if report['connect_command']:
        tell('Connect: ' + shlex.join(report['connect_command']))
