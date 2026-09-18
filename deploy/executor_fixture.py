#!/usr/bin/python3
"""Fixed synthetic executor payload. No model calls, repository code or real secrets."""
import json
import hashlib
import os
from pathlib import Path
import socket
import sys
import time

WORKSPACE = Path('/home/forge/workspace')


def write_report(name, value):
    destination = WORKSPACE / name
    temporary = destination.with_name('.' + name + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True))
    os.replace(temporary, destination)


def probe(provider):
    report = {'uid': os.getuid(), 'gid': os.getgid(), 'groups': os.getgroups(), 'checks': {}}
    checks = report['checks']
    target = WORKSPACE / 'fixture-edit.txt'
    target.write_text('synthetic edit\n')
    checks['workspace_write'] = target.read_text() == 'synthetic edit\n'
    # These paths contain only deliberately created test markers. No actual
    # credentials are opened, even if a sandbox property is accidentally missing.
    for name, path in {
        'host_marker_hidden': '/var/lib/forge-executor/host-marker',
        'operator_marker_hidden': str(Path.home() / '.forge-executor-dummy-marker'),
        'other_attempt_hidden': '/var/lib/forge-executor/attempts/sibling/marker',
    }.items():
        try:
            Path(path).read_bytes()
            checks[name] = False
        except OSError:
            checks[name] = True
    policy = Path({'codex': '/etc/codex/requirements.toml', 'grok': '/etc/grok/requirements.toml'}
                  .get(provider, '/etc/claude-code/managed-settings.json'))
    checks['managed_policy_present'] = policy.is_file()
    try:
        policy_bytes = policy.read_bytes()
        checks['managed_policy_readable'] = bool(policy_bytes)
        report['policy_sha256'] = hashlib.sha256(policy_bytes).hexdigest()
    except OSError:
        checks['managed_policy_readable'] = False
    try:
        fd = os.open(policy, os.O_WRONLY | os.O_APPEND)
        os.close(fd)
        checks['managed_policy_immutable'] = False
    except OSError:
        checks['managed_policy_immutable'] = checks['managed_policy_present']
    try:
        policy.unlink()
        checks['managed_policy_cannot_unlink'] = False
    except OSError:
        checks['managed_policy_cannot_unlink'] = checks['managed_policy_present']
    for name, path in {
        'system_bus_hidden': '/run/dbus/system_bus_socket',
        'systemd_socket_hidden': '/run/systemd/private',
        'docker_socket_hidden': '/run/docker.sock',
    }.items():
        checks[name] = not Path(path).exists()
    client = None
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(0.25)
        client.connect(('127.0.0.1', 41987))
        checks['host_listener_inaccessible'] = False
    except OSError:
        checks['host_listener_inaccessible'] = True
    finally:
        if client is not None:
            client.close()
    report['pid'] = os.getpid()
    write_report('fixture-report.json', report)
    time.sleep(2)  # bounded window for the trusted launcher to persist service identity
    return 0 if all(checks.values()) else 1


def linger():
    child = os.fork()
    if child == 0:
        os.setsid()
        grandchild = os.fork()
        if grandchild:
            os._exit(0)
        write_report('descendant.json', {'pid': os.getpid(), 'pgid': os.getpgrp()})
        while True:
            time.sleep(0.1)
    os.waitpid(child, 0)
    write_report('leader.json', {'pid': os.getpid(), 'pgid': os.getpgrp()})
    while True:
        time.sleep(0.1)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in {'probe', 'linger'} or sys.argv[2] not in {'codex', 'claude', 'grok'}:
        raise SystemExit('fixed synthetic mode required')
    if sys.argv[1] == 'linger':
        linger()
    raise SystemExit(probe(sys.argv[2]))
