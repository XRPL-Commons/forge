"""Conservative admission for the legacy tmux fleet on the 8 GiB host.

Compatibility control, not a resource reservation or process isolation boundary.
The supervisor singleton must serialize check + launch. Count every configured
role session, including idle and disabled-repository survivors. Personal sessions
are not counted, but their memory use influences host MemAvailable and PSI.
One slot per role preserves reviewer/auditor progression; builders are serialized
across repositories. Retained idle sessions still consume slots: reclaiming them
needs explicit lifecycle evidence, never guessed inactivity or forced teardown.
The memory floor is an admission guard, not a reservation for a 6 GiB build peak.
This module never captures terminal panes, kills sessions, or reads process args.
"""
import argparse
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import re
import subprocess
import time

from .config import load_repositories
from .control import atomic_json

MIB = 1024 * 1024
ROLES = ('build', 'review', 'audit')


@dataclass(frozen=True)
class Limits:
    max_sessions: int = 3
    max_sessions_per_role: int = 1
    minimum_available_mib: int = 2048
    launch_reserve_mib: int = 512
    max_memory_psi_some: float = 1.0
    max_memory_psi_full: float = 0.1

    def __post_init__(self):
        for name in ('max_sessions', 'max_sessions_per_role', 'minimum_available_mib', 'launch_reserve_mib'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be a positive integer')
        for name in ('max_memory_psi_some', 'max_memory_psi_full'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f'{name} must be a finite percentage in [0,100]')


def read_sessions(run=subprocess.run):
    result = run(['tmux', 'list-sessions', '-F', '#{session_name}'],
                 capture_output=True, text=True, timeout=10)
    if result.returncode:
        absent = (re.fullmatch(r'no server running on [^\n]+', result.stderr.strip()) or
                  re.fullmatch(r'error connecting to [^\n]+ \(No such file or directory\)', result.stderr.strip()) or
                  result.stderr.strip() == 'no sessions')
        if result.returncode == 1 and not result.stdout.strip() and absent:
            return [], 'server_absent_or_empty'
        raise ValueError('tmux state unavailable')
    names = result.stdout.splitlines()
    if (not names or len(set(names)) != len(names) or
            any(not name or any(ord(char) < 32 for char in name) for name in names)):
        raise ValueError('malformed tmux session listing')
    return names, 'observed'


def parse_meminfo(text):
    values = {}
    for line in text.splitlines():
        name, separator, raw = line.partition(':')
        if name not in {'MemTotal', 'MemAvailable'}:
            continue
        match = re.fullmatch(r'\s*([0-9]+)\s+kB\s*', raw)
        if not separator or name in values or not match:
            raise ValueError('malformed memory telemetry')
        values[name] = int(match[1]) * 1024
    if (set(values) != {'MemTotal', 'MemAvailable'} or values['MemTotal'] <= 0 or
            not 0 <= values['MemAvailable'] <= values['MemTotal']):
        raise ValueError('missing or inconsistent memory telemetry')
    return values


def parse_pressure(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        kind = fields[0]
        if kind not in {'some', 'full'} or kind in result:
            raise ValueError('malformed memory pressure telemetry')
        row = {}
        for field in fields[1:]:
            name, separator, raw = field.partition('=')
            if not separator or name in row or name not in {'avg10', 'avg60', 'avg300', 'total'}:
                raise ValueError('malformed pressure field')
            if name == 'total':
                if not raw.isascii() or not raw.isdigit():
                    raise ValueError('invalid pressure total')
                row[name] = int(raw)
            else:
                value = float(raw)
                if not math.isfinite(value) or not 0 <= value <= 100:
                    raise ValueError('invalid pressure percentage')
                row[name] = value
        if set(row) != {'avg10', 'avg60', 'avg300', 'total'}:
            raise ValueError('missing pressure field')
        result[kind] = row
    if set(result) != {'some', 'full'}:
        raise ValueError('missing memory pressure telemetry')
    return result


def observe(config, session, *, limits=None, run=subprocess.run, read_text=None, now=None):
    limits = limits or Limits()
    read_text = read_text or (lambda path: Path(path).read_text())
    decision = {'schema': 1, 'mode': 'legacy_admission_only', 'observed_at': time.time() if now is None else now,
                'requested_session': session, 'admitted': False, 'limits': asdict(limits),
                'reason': 'telemetry_unavailable'}
    try:
        if not Path(config).is_dir():
            raise ValueError('repository configuration directory missing')
        repositories = load_repositories(config)
        if not repositories:
            raise ValueError('repository configuration empty')
        configured = {r.short + '-' + role for r in repositories for role in ROLES}
        enabled = {r.short + '-' + role for r in repositories if r.enabled for role in ROLES}
        if session not in configured:
            decision['reason'] = 'session_not_configured'
            return decision
        names, source = read_sessions(run)
        fleet = sorted(set(names) & configured)
        role_counts = {role: sum(name.endswith('-' + role) for name in fleet) for role in ROLES}
        requested_role = session.rsplit('-', 1)[1]
        memory = parse_meminfo(read_text('/proc/meminfo'))
        pressure = parse_pressure(read_text('/proc/pressure/memory'))
        remaining = memory['MemAvailable'] - limits.launch_reserve_mib * MIB
        decision.update(fleet_sessions=fleet, fleet_count=len(fleet), role_counts=role_counts,
                        requested_role=requested_role, tmux_status=source,
                        disabled_survivors=sorted(set(fleet) - enabled),
                        memory_available_bytes=memory['MemAvailable'], memory_total_bytes=memory['MemTotal'],
                        memory_after_launch_reserve_bytes=remaining, memory_pressure=pressure)
        if session not in enabled:
            decision['reason'] = 'repository_disabled'
        elif session in fleet:
            decision['reason'] = 'session_already_exists'
        elif len(fleet) >= limits.max_sessions:
            decision['reason'] = 'fleet_session_limit'
        elif role_counts[requested_role] >= limits.max_sessions_per_role:
            decision['reason'] = 'role_session_limit'
        elif remaining < limits.minimum_available_mib * MIB:
            decision['reason'] = 'memory_headroom'
        elif any(pressure[kind][window] > maximum
                 for kind, maximum in (('some', limits.max_memory_psi_some), ('full', limits.max_memory_psi_full))
                 for window in ('avg10', 'avg60')):
            decision['reason'] = 'memory_pressure'
        else:
            decision.update(admitted=True, reason='capacity_available')
    except (OSError, ValueError, subprocess.SubprocessError, TypeError, KeyError) as error:
        decision.update(admitted=False, reason='telemetry_unavailable', error_type=type(error).__name__)
    return decision


def check(config, state, session, **kwargs):
    decision = observe(config, session, **kwargs)
    # Persist before granting admission so the dashboard cannot silently miss it.
    # This is the last decision, not a lease, reservation, or continuous monitor.
    try:
        atomic_json(Path(state) / 'capacity.json', decision)
    except OSError as error:
        decision.update(admitted=False, reason='decision_persistence_failed', error_type=type(error).__name__)
    return decision


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--max-sessions', type=int, default=3)
    parser.add_argument('--max-sessions-per-role', type=int, default=1)
    parser.add_argument('--minimum-available-mib', type=int, default=2048)
    parser.add_argument('--launch-reserve-mib', type=int, default=512)
    parser.add_argument('--max-memory-psi-some', type=float, default=1.0)
    parser.add_argument('--max-memory-psi-full', type=float, default=0.1)
    args = parser.parse_args(argv)
    try:
        limits = Limits(max_sessions=args.max_sessions, max_sessions_per_role=args.max_sessions_per_role,
                        minimum_available_mib=args.minimum_available_mib, launch_reserve_mib=args.launch_reserve_mib,
                        max_memory_psi_some=args.max_memory_psi_some, max_memory_psi_full=args.max_memory_psi_full)
    except ValueError as error:
        parser.error(str(error))
    decision = check(args.config, args.state, args.session, limits=limits)
    import json
    print(json.dumps(decision, sort_keys=True))
    return 0 if decision['admitted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
