"""One controller lease shared by the setup supervisor and this CLI; durable operator requests."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time

from ..settings import Settings, digest, write_json, read_json
from ..state import AtomicStore, StateError
from ..credentials import Credentials
from .config import load, validate
from .contracts import summary as plain_summary
from .github import GitHub, GitHubUnavailable
from .ledger import Ledger
from .runner import ContainerRunner
from .supervisor import Supervisor


class ControllerBusy(StateError):
    pass


@contextmanager
def controller_lock(root, *, timeout=0):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / 'state-supervisor.lock', 'a') as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControllerBusy('Another supervisor owns this factory; drain it before cutover') from None
                time.sleep(min(0.2, remaining))
        yield


def queue(root, action, target, reason):
    if action == 'retry':
        raise ValueError('Open the issue in Issues to review and retry its failed attempt.')
    if action not in ('stop', 'reset') or not isinstance(reason, str) or not reason.strip():
        raise ValueError('Action and reason are required')
    if not isinstance(target, str) or not target:
        raise ValueError('Target identity is required')
    import uuid
    store = AtomicStore(Path(root) / 'state' / 'factory', 'operator-requests')
    identifier = str(uuid.uuid4())
    with store.transaction() as data:
        data[identifier] = {'action': action, 'target': target, 'reason': reason[:4096], 'status': 'pending'}
    return identifier


def requests(root, ledger):
    store = AtomicStore(Path(root) / 'state' / 'factory', 'operator-requests')
    with store.transaction() as data:
        for identifier, request in data.items():
            if request['status'] != 'pending':
                continue
            try:
                snapshot = ledger.read()
                if request['action'] == 'retry':
                    # The operator request ID makes interrupted processing repeat-safe.
                    if not any(e.get('request') == identifier for e in snapshot['events']):
                        if snapshot['issues'][request['target']]['state'] != 'failed':
                            raise ValueError('Retry is only for failed attempts.')
                        ledger.retry(request['target'], request['reason'], request_id=identifier)
                else:
                    job = snapshot['jobs'][request['target']]
                    if not job['cleanup']:
                        if job['outcome'] is None:
                            if job['role'] == 'recovery':
                                stopped = Supervisor.incomplete('Investigation stopped by operator')
                                ledger.findings(job['id'], stopped, summary=plain_summary(stopped, 'recovery', time.time()))
                            else:
                                ledger.cancel(job['issue_key'], request['reason'])
                        ledger.job_update(job['id'], fenced=True, phase='stopping')
                request['status'] = 'applied'
            except (StateError, ValueError, KeyError) as error:
                request.update(status='rejected', error=str(error))
    if (Path(root) / 'state/issues/actions.json').exists():
        from .issue_actions import IssueActions
        IssueActions(root, ledger).process()


def deployments(root):
    """The last observation per repository; a corrupt store never fails the page."""
    try:
        rows = read_json(Path(root) / 'state/deployments.json')['repositories']
        return {key: {'name': row.get('name'), **{field: row['observed'].get(field)
                                                  for field in ('live', 'deploy', 'error')}}
                for key, row in rows.items()
                if isinstance(row, dict) and isinstance(row.get('observed'), dict)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def view(root):
    ledger = Ledger(Path(root) / 'state' / 'factory')
    data = ledger.read()
    # Activity is a summary, not a second copy of the durable ledger. Full
    # reports stay in the ledger and the issue/output endpoints.
    issue_fields = ('key', 'repo_id', 'repo', 'number', 'title', 'state', 'reason',
                    'active', 'last_summary', 'reviewers_requested', 'prerequisite_hold',
                    'blocked_dependencies', 'dependency_reason', 'ownership_wait', 'created_at', 'last_observed',
                    'retry_after', 'deployed', 'deploy_failed')
    histories = {}
    for event in data['events']:
        histories.setdefault(event['issue'], []).append(event)
    issues = []
    for item in data['issues'].values():
        row = {key: item[key] for key in issue_fields if key in item}
        row['failure_count'] = ledger.failure_count(item)
        row['reason'] = str(row['reason'])[:1000] if row.get('reason') else None
        row.setdefault('last_summary', None)
        candidate = item.get('candidate')
        row['candidate'] = {key: candidate[key] for key in ('pr', 'head', 'merge_commit') if key in candidate} if candidate else None
        history = histories.get(item['key'], [])
        recent = history[-3:]
        merged = next((event for event in reversed(history) if event['kind'] == 'merged'), None)
        # Merge timestamps drive the dashboard's last-day/week counts, even
        # when later deployment events have pushed the merge out of the tail.
        if merged is not None and merged not in recent:
            recent = [merged, *recent]
        row['history'] = [{key: event[key] for key in ('at', 'kind', 'provider', 'model') if key in event}
                          for event in recent]
        row.setdefault('deployed', None)
        row.setdefault('deploy_failed', None)
        issues.append(row)
    job_fields = ('id', 'issue_key', 'repo_id', 'repo', 'issue_number', 'role', 'slot',
                  'provider', 'provider_id', 'model', 'phase', 'outcome', 'cleanup', 'fenced',
                  'capacity_wait_since', 'created_at', 'deadline', 'last_observed')
    jobs = [{key: j[key] for key in job_fields if key in j}
            for j in data['jobs'].values() if not j['cleanup'] or j['outcome'] is None]
    for job in jobs:
        observed = data['jobs'][job['id']].get('observation') or {}
        job['observed_at'] = observed.get('at')
        job['started_at'] = (observed.get('execution') or {}).get('started_at')
    with AtomicStore(Path(root) / 'state' / 'factory', 'operator-requests').transaction() as requests_data:
        actions = [{'id': key, **value} for key, value in list(requests_data.items())[-10:]]
    try:
        observation = read_json(Path(root) / 'state' / 'factory' / 'supervisor.json')
    except (OSError, ValueError):
        observation = {}
    from .operator_status import overview
    repositories = Settings(root).view()['repositories']
    runtime = load(root)
    active = (Path(root) / 'state' / 'factory-v2.enabled').exists()
    operator = overview(data, repositories, runtime, observation, active, time.time())
    return {'operator': operator, 'repositories': repositories, 'observation': observation, 'actions': actions, 'issues': issues, 'jobs': jobs, 'slots': data['slots'], 'runtime': runtime,
            'deployments': deployments(root),
            'runtime_revision': digest(runtime), 'active': active}


def select_view(payload, *, section=None, issue=None, now=None):
    """Small Activity view plus independently fetched, collapsed card sections."""
    if section not in (None, 'board', 'completed', 'runners') or (section and issue):
        raise ValueError('Invalid factory view')
    issues, jobs = payload['issues'], payload['jobs']
    # Readiness is already summarized by operator_status. Detailed protection,
    # database evidence and deployment proposals belong on repository pages.
    repositories = [{key: row[key] for key in ('id', 'name', 'enabled', 'deployment') if key in row}
                    for row in payload['repositories']]
    for row in repositories:
        if 'deployment' in row:
            row['deployment'] = {'profile': (row['deployment'] or {}).get('profile')}
    payload = {**payload, 'repositories': repositories}
    if issue is not None:
        return {**payload, 'issues': [row for row in issues if row['key'] == issue],
                'jobs': [row for row in jobs if row['issue_key'] == issue]}
    if section is None:
        return payload
    completed = [row for row in issues if row['state'] in ('merged', 'cancelled') and not row.get('active')]
    investigating = [row for row in issues if row['state'] == 'investigating']
    active = {row.get('active') for row in issues}
    runner_ids = {row.get('active') for row in investigating}
    runners = [job for job in jobs if job['id'] not in active or job['id'] in runner_ids]
    if section == 'completed':
        return {**payload, 'issues': completed, 'jobs': []}
    if section == 'runners':
        return {**payload, 'issues': investigating, 'jobs': runners}
    now = time.time() if now is None else now
    merged = [row for row in issues if row['state'] == 'merged']
    tracked = {row['id'] for row in repositories if (row.get('deployment') or {}).get('profile')}
    def merged_at(row):
        return next((event['at'] for event in reversed(row['history']) if event['kind'] == 'merged'), 0)
    summary = {
        'completed_count': len(completed),
        'runner_count': len(investigating) + sum(job['id'] not in active for job in runners),
        'runners_running': any(job.get('phase') == 'running' for job in runners),
        'workers': [{key: job.get(key) for key in ('role', 'phase', 'cleanup', 'fenced', 'outcome', 'observed_at', 'last_observed')} for job in jobs],
        'merged_day': sum(merged_at(row) >= now - 86400 for row in merged),
        'merged_week': sum(merged_at(row) >= now - 604800 for row in merged),
        'merged_total': len(merged),
        'deployed_day': sum(bool(row.get('deployed') and row['deployed']['at'] >= now - 86400) for row in issues),
        'waiting': sum(not row.get('deployed') and not row.get('deploy_failed') and row['repo_id'] in tracked for row in merged),
        'deploy_failed': sum(bool(row.get('deploy_failed') and not row.get('deployed')) for row in issues),
    }
    hidden = {row['key'] for row in [*completed, *investigating]}
    visible = [row for row in issues if row['key'] not in hidden]
    visible_jobs = {row.get('active') for row in visible}
    return {**payload, 'issues': visible, 'jobs': [job for job in jobs if job['id'] in visible_jobs], 'summary': summary}


def save_runtime(root, config, revision):
    config = validate(config)
    with Settings(root).locked('runtime-config'):
        if revision != digest(load(root)):
            raise ValueError('Runtime settings changed; reload before saving')
        write_json(Path(root) / 'runtime.json', config)
    return view(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'validate', 'activate', 'once', 'run', 'stop', 'reset', 'retry',
                                          'environment-probe', 'environment-confirm'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--target')
    parser.add_argument('--reason', default='')
    parser.add_argument('--revision', help='Exact environment proposal revision to confirm')
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.command in ('stop', 'reset', 'retry'):
            print(json.dumps({'request': queue(root, args.command, args.target, args.reason)}))
            return 0
        if args.command == 'status':
            print(json.dumps(view(root), indent=2))
            return 0
        settings, config = Settings(root), load(root)
        if args.command in ('environment-probe', 'environment-confirm'):
            from .environments import probe
            repo = next((r for r in settings.view()['repositories'] if str(r['id']) == args.target), None)
            if not repo:
                raise ValueError('Use --target with a connected repository ID')
            store = AtomicStore(root / 'state', 'environment-proposals')
            if args.command == 'environment-probe':
                value = probe(GitHub(settings, config), {'repo': repo['name'], 'repo_id': repo['id']})
                with store.transaction() as data:
                    data[args.target] = value
                print(json.dumps(value, indent=2))
            else:
                with settings.locked('runtime-config'), store.transaction() as data:
                    proposal = data.get(args.target)
                    if not proposal or not args.revision or proposal['revision'] != args.revision:
                        raise ValueError('Probe the repository and confirm its exact --revision')
                    current = load(root)
                    current['repository_environments'] = {**current['repository_environments'], args.target: {
                        **{k: proposal[k] for k in ('services', 'evidence', 'head')}, 'confirmed_at': time.time(), 'default': True}}
                    write_json(root / 'runtime.json', validate(current))
                print('Optional repository default saved. Issue environment choices take precedence.')
            return 0
        if args.command == 'validate':
            settings.load()
            print('Runtime configuration is valid. Access and execution preflight are separate checks.')
            return 0
        with controller_lock(root):
            marker = root / 'state' / 'factory-v2.enabled'
            ledger = Ledger(root / 'state' / 'factory')
            if args.command == 'activate':
                if not settings.path.exists():
                    raise ValueError('Configure GitHub Apps before activating the new controller')
                ledger.read()  # Initialize durable ownership before marking the mode.
                write_json(marker, {'version': 1, 'activated_at': time.time()})
                print('Factory controller selected. runtime.json still controls dispatch and merge.')
                return 0
            if not marker.exists():
                raise ValueError('Factory controller is not selected; finish setup or run activate first')
            last_sync = 0
            while True:
                if (root / 'state/factory-paused.json').exists():
                    raise ValueError('Factory is paused for reset; use the setup supervisor')
                config = load(root)
                if args.command == 'run' and time.time() - last_sync >= settings.load()['sync_interval_seconds']:
                    settings.start_refresh(); last_sync = time.time()
                runner = ContainerRunner(config, environ=Credentials(root).environment())
                github = GitHub(settings, config)
                supervisor = Supervisor(ledger, runner, github, settings, config)
                requests(root, ledger)
                observation = supervisor.cycle()
                write_json(root / 'state' / 'factory' / 'supervisor.json', observation)
                if args.command == 'once':
                    print(json.dumps(observation))
                    return 0 if not observation['errors'] else 1
                time.sleep(config['cycle_seconds'])
    except (ValueError, StateError, OSError, GitHubUnavailable) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
