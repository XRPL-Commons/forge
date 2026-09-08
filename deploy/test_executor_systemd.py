#!/usr/bin/python3
"""Bounded root-only synthetic executor canary; dry-run unless explicitly applied.

Uses existing forge-codex/forge-claude accounts. No real auth, repository code,
GitHub writes, persistent units, sudoers rules or global CLI settings are installed.
"""
import argparse
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

SOURCE = Path(__file__).resolve().parents[1]
FILES = ['forge/executor.py', 'deploy/executor_fixture.py'] + [
    'deploy/executor-policies/' + name for name in (
        'codex/requirements.toml', 'codex/config.toml',
        'claude/managed-settings.json', 'claude/managed-mcp.json', 'launcher-contract.json')]


def bundle():
    values = {name: (SOURCE / name).read_bytes() for name in FILES}
    manifest = {name: hashlib.sha256(value).hexdigest() for name, value in values.items()}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return values, manifest, digest


def directory(path, mode=0o755):
    path = Path(path)
    if not path.exists():
        directory(path.parent)
        path.mkdir(mode=mode)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('Unsafe root-owned directory: ' + str(path))
    return path


def new_file(path, data, mode=0o644):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.' + str(uuid.uuid4()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # exclusive publication of complete bytes
    finally:
        temporary.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError('Bounded canary wait expired')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--bundle-sha256')
    args = parser.parse_args()
    data, manifest, digest = bundle()
    if not args.apply:
        print(json.dumps({'mode': 'dry_run', 'bundle_sha256': digest, 'files': manifest,
            'accounts': ['forge-codex', 'forge-claude'],
            'scope': 'temporary dummy fixtures and transient system services only'}, indent=2))
        return
    if os.geteuid() != 0 or args.bundle_sha256 != digest:
        raise SystemExit('Root and the exact reviewed bundle SHA256 are required')
    root = directory('/var/lib/forge-executor')
    attempts = directory(root / 'attempts')
    run = directory(root / ('canary-' + str(uuid.uuid4())))
    new_file(run / 'executor.py', data['forge/executor.py'])
    spec = importlib.util.spec_from_file_location('protected_canary_executor', run / 'executor.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    fixture_dir = directory('/opt/forge-executor')
    fixture = fixture_dir / 'fixture.py'
    created_fixture = not fixture.exists()
    if created_fixture:
        new_file(fixture, data['deploy/executor_fixture.py'], 0o755)
    elif fixture.is_symlink() or fixture.read_bytes() != data['deploy/executor_fixture.py'] or fixture.stat().st_uid != 0:
        raise RuntimeError('Existing fixture differs; no overwrite allowed')
    records, active, prepared, markers, launching = [], [], [], [], []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = {'bundle_sha256': digest, 'manifest': manifest, 'synthetic_only': True, 'checks': records}
    receipt_path = run / 'receipt.json'
    executor = module.SystemdExecutor()

    def prepare(provider):
        plan = module.AttemptPlan.fresh(provider)
        directory(plan.directory)
        prepared.append(plan)
        account = pwd.getpwnam(plan.user)
        for child in ('workspace', 'client'):
            p = plan.directory / child
            p.mkdir(mode=0o700)
            os.chown(p, account.pw_uid, account.pw_gid)
        policy = directory(directory(plan.directory / 'policy') / provider)
        for name in module.POLICY_FILES[provider]:
            new_file(policy / name, data['deploy/executor-policies/' + provider + '/' + name])
        return plan

    def save():
        receipt_path.write_text(json.dumps(result, indent=2) + '\n')

    def launched(plan, scenario):
        launching.append(plan)
        receipt = executor.start_synthetic(plan, scenario=scenario, apply=True)
        active.append((plan, receipt))
        new_file(plan.directory / 'receipt.json', json.dumps(asdict(receipt)).encode())
        return receipt

    try:
        listener.bind(('127.0.0.1', 41987))
        listener.listen(8)
        listener.settimeout(0.05)
        sibling = directory(attempts / 'sibling')
        for path in (root / 'host-marker', Path.home() / '.forge-executor-dummy-marker', sibling / 'marker'):
            new_file(path, b'ONLY A SYNTHETIC HOST MARKER\n')
            assert path.read_bytes() == b'ONLY A SYNTHETIC HOST MARKER\n'
            markers.append(path)
        for provider in ('codex', 'claude'):
            plan = prepare(provider)
            receipt = launched(plan, 'probe')
            report_path = plan.directory / 'workspace/fixture-report.json'
            wait_for(report_path.exists)
            report = json.loads(report_path.read_text())
            account = pwd.getpwnam(plan.user)
            assert report['uid'] == account.pw_uid and report['gid'] == account.pw_gid
            assert set(report['groups']) <= {account.pw_gid}
            assert all(report['checks'].values()), report
            policy_name = 'requirements.toml' if provider == 'codex' else 'managed-settings.json'
            assert report['policy_sha256'] == manifest['deploy/executor-policies/' + provider + '/' + policy_name]
            final = wait_for(lambda: (v if (v := executor.reconcile(plan, receipt))['cleanup_verified'] else None))
            assert final['exit_status'] in ('0', None), final
            records.append({'provider': provider, 'probe': report, 'cleanup': final})
            save()
        plan = prepare('codex')
        launching.append(plan)
        controller_pid = os.fork()
        if controller_pid == 0:
            try:
                receipt = executor.start_synthetic(plan, scenario='linger', apply=True)
                new_file(plan.directory / 'receipt.json', json.dumps(asdict(receipt)).encode())
                while True:
                    time.sleep(0.1)
            except BaseException:
                os._exit(1)
        try:
            wait_for((plan.directory / 'receipt.json').exists)
            receipt = module.Receipt(**json.loads((plan.directory / 'receipt.json').read_text()))
            active.append((plan, receipt))
        finally:
            os.kill(controller_pid, signal.SIGKILL)
            os.waitpid(controller_pid, 0)
        child_path = plan.directory / 'workspace/descendant.json'
        wait_for(lambda: child_path.exists() and (plan.directory / 'workspace/leader.json').exists())
        child = json.loads(child_path.read_text())
        leader = json.loads((plan.directory / 'workspace/leader.json').read_text())
        assert child['pgid'] != leader['pgid']
        assert Path('/proc', str(child['pid']), 'cgroup').read_text().strip().endswith(receipt.cgroup)
        # Simulate recovery using a fresh controller object and persisted identity.
        fresh = module.SystemdExecutor()
        persisted = module.Receipt(**json.loads((plan.directory / 'receipt.json').read_text()))
        assert not fresh.reconcile(plan, persisted)['cleanup_verified']
        final = fresh.cancel(plan, persisted, apply=True)
        records.append({'case': 'controller_sigkill_restart_reconcile_setsid_doublefork', 'cleanup': final})
        save()
        plan = prepare('claude')
        receipt = launched(plan, 'linger')
        wait_for((plan.directory / 'workspace/descendant.json').exists)
        # No controller stop: manager-owned RuntimeMaxSec must end the whole group.
        final = wait_for(lambda: (v if (v := executor.reconcile(plan, receipt))['cleanup_verified'] else None), 26)
        records.append({'case': 'unattended_runtime_deadline', 'cleanup': final})
        try:
            connection, _ = listener.accept()
            connection.close()
            raise RuntimeError('Host listener received a connection from isolated fixture')
        except socket.timeout:
            pass
        result['passed'] = True
    except BaseException as error:
        result.update(passed=False, error=type(error).__name__ + ': ' + str(error))
        raise
    finally:
        cleanup = []
        for plan, receipt in active:
            try:
                cleanup.append(executor.cancel(plan, receipt, apply=True))
            except Exception as error:
                cleanup.append({'unit': plan.unit, 'cleanup_verified': False, 'error': str(error)})
        known = {plan.attempt_id for plan, receipt in active}
        for plan in launching:
            if plan.attempt_id not in known:
                cleanup.append({'unit': plan.unit, 'cleanup_verified': False,
                                'error': 'Start identity unknown; artifacts retained, RuntimeMaxSec remains enforced by manager'})
        result['final_cleanup'] = cleanup
        if not all(r.get('cleanup_verified') for r in cleanup):
            result.update(passed=False, cleanup_error='Cleanup unverified; retained artifacts require reconciliation')
        save()
        listener.close()
        for path in markers:
            path.unlink()
        # Preserve attempt artifacts and receipts for review. No cleanup is guessed.
        if created_fixture and all(r.get('cleanup_verified') for r in cleanup):
            fixture.unlink()
        print(json.dumps({'receipt': str(receipt_path), 'passed': result.get('passed', False),
                          'cleanup_verified': all(r.get('cleanup_verified') for r in cleanup)}))

    if not result.get('passed'):
        raise RuntimeError('Canary or final cleanup failed; see protected receipt')


if __name__ == '__main__':
    main()
