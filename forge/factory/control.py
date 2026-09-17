"""One controller lease shared with the legacy supervisor; durable operator requests."""
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
from .github import GitHub
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
    if action not in ('stop', 'reset', 'retry') or not isinstance(reason, str) or not reason.strip():
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
                        ledger.retry(request['target'], request['reason'], request_id=identifier)
                else:
                    job = snapshot['jobs'][request['target']]
                    if not job['cleanup']:
                        if job['outcome'] is None:
                            if job['role'] == 'recovery':
                                ledger.findings(job['id'], Supervisor.incomplete('Investigation stopped by operator'))
                            else:
                                ledger.cancel(job['issue_key'], request['reason'])
                        ledger.job_update(job['id'], fenced=True, phase='stopping')
                request['status'] = 'applied'
            except (StateError, ValueError, KeyError) as error:
                request.update(status='rejected', error=str(error))


def view(root):
    ledger = Ledger(Path(root) / 'state' / 'factory')
    data = ledger.read()
    # Logs are available per execution rather than duplicating them on every card.
    issues = []
    for item in data['issues'].values():
        row = {**item, 'failure_count': ledger.failure_count(item)}
        row['history'] = [e for e in data['events'] if e['issue'] == item['key']][-50:]
        row['failures'] = [{k: v for k, v in f.items() if k != 'evidence'} for f in item['failures']]
        issues.append(row)
    jobs = [{k: v for k, v in j.items() if k not in ('capture', 'observation', 'history', 'failure')}
            for j in data['jobs'].values() if not j['cleanup'] or j['outcome'] is None]
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
            'runtime_revision': digest(runtime), 'active': active}


def save_runtime(root, config, revision):
    config = validate(config)
    with Settings(root).locked('runtime-config'):
        if revision != digest(load(root)):
            raise ValueError('Runtime settings changed; reload before saving')
        write_json(Path(root) / 'runtime.json', config)
    return view(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'validate', 'activate', 'once', 'run', 'stop', 'reset', 'retry'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--target')
    parser.add_argument('--reason', default='')
    parser.add_argument('--legacy-drained', action='store_true', help='Confirm all legacy executions have been stopped and reconciled')
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
        if args.command == 'validate':
            settings.load()
            print('Runtime configuration is valid. Access and execution preflight are separate checks.')
            return 0
        with controller_lock(root):
            marker = root / 'state' / 'factory-v2.enabled'
            ledger = Ledger(root / 'state' / 'factory')
            if args.command == 'activate':
                if not args.legacy_drained:
                    raise ValueError('Drain legacy workers and confirm --legacy-drained before changing controller mode')
                if not settings.path.exists():
                    raise ValueError('Configure GitHub Apps before activating the new controller')
                ledger.read()  # Initialize durable ownership before marking the mode.
                write_json(marker, {'version': 1, 'activated_at': time.time(), 'legacy_drained': True})
                print('Factory controller selected. runtime.json still controls dispatch and merge.')
                return 0
            if not marker.exists():
                raise ValueError('New controller is not selected; run activate after draining legacy workers')
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
    except (ValueError, StateError, OSError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
