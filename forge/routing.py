"""Global harness alternation with sticky, hour-long provider outage records.

Selection reads local availability only. Expired outages permit a single leased
attempt, rather than an account probe for every queued role. Task failures and
security findings never open a provider outage.
"""
import argparse
import json
import os
import math
from pathlib import Path
import shutil
import subprocess
import time

from .config import load_repositories
from .lifecycle import generation, observe_generation
from .state import AtomicStore, StateError

PROVIDERS = ('claude', 'codex')
OUTAGES = {'blocked_model_usage', 'blocked_quota', 'blocked_auth'}
NORMALIZED = {'usage_limit': 'blocked_model_usage', 'authentication': 'blocked_auth'}
COOLDOWN = 3600
PROBE_LEASE = 180


def validate(data):
    if not isinstance(data, dict):
        raise StateError('Invalid routing state')
    for key in ('providers', 'bindings', 'selections', 'seen_failures'):
        if key in data and not isinstance(data[key], dict):
            raise StateError('Invalid routing records')
    for key in ('last_provider', 'next_provider'):
        if key in data and data[key] not in PROVIDERS:
            raise StateError('Invalid provider preference')
    for name, circuit in data.get('providers', {}).items():
        if name not in PROVIDERS or not isinstance(circuit, dict):
            raise StateError('Invalid provider circuit')
        for key in ('blocked_until', 'probe_until', 'last_failure'):
            value = circuit.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise StateError('Invalid provider deadline')
        if 'probe_generation' in circuit:
            value = circuit['probe_generation']
            if not isinstance(value, dict) or generation(value.get('id')) != value or not isinstance(circuit.get('probe_session'), str) or not circuit['probe_session']:
                raise StateError('Invalid probe identity')
    for value in data.get('bindings', {}).values():
        if not isinstance(value, dict) or value.get('provider') not in PROVIDERS or not isinstance(value.get('generation'), dict) or generation(value['generation'].get('id')) != value['generation']:
            raise StateError('Invalid provider binding')
        at = value.get('bound_at')
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at < value['generation']['started_at']:
            raise StateError('Invalid binding time')
    for value in data.get('seen_failures', {}).values():
        if not isinstance(value, (float, int)) or not math.isfinite(value):
            raise StateError('Invalid failure receipt')
    return data


def choose(data, available, session, now, live_generation=None):
    validate(data)
    circuits = data.setdefault('providers', {})
    preferred = data.get('next_provider') or ('codex' if data.get('last_provider') == 'claude' else 'claude')
    ordered = (preferred, 'codex' if preferred == 'claude' else 'claude')
    for provider in ordered:
        if provider not in available:
            continue
        circuit = circuits.get(provider, {})
        if circuit.get('blocked_until', 0) > now:
            continue
        if circuit.get('probe_until', 0) > now:
            continue
        # Only the selected real execution probes an expired circuit. Its live
        # generation owns that lease until an outcome; never probe per run.
        probe_identity = circuit.get('probe_generation')
        if probe_identity:
            observed = live_generation(circuit.get('probe_session')) if live_generation else None
            if observed is None or observed == probe_identity:
                continue
        probe = bool(circuit.get('last_failure'))
        if probe:
            circuit.update(probe_until=now + PROBE_LEASE, probe_session=session)
            circuit.pop('probe_generation', None)
        result = {'provider': provider, 'reason': 'hourly_probe' if probe else
                  'alternating_dispatch' if provider == preferred else 'alternate_provider_available',
                  'probe': probe, 'selected_at': now,
                  'preferred_provider': preferred, 'degraded': provider != preferred}
        data.setdefault('selections', {})[session] = result
        return result
    retries = [max(circuits.get(p, {}).get('blocked_until', 0), circuits.get(p, {}).get('probe_until', 0))
               for p in available]
    result = {'provider': None, 'reason': 'no_provider_available', 'selected_at': now,
              'retry_at': min((n for n in retries if n > now), default=None)}
    data.setdefault('selections', {})[session] = result
    return result


def bind(data, session, provider, identity, now):
    if provider not in PROVIDERS or generation(identity.get('id')) != identity:
        raise ValueError('Invalid provider binding')
    existing = data.setdefault('bindings', {}).get(session, {})
    if existing.get('generation') == identity:
        if existing.get('provider') != provider:
            raise ValueError('Generation is already bound to another provider')
        return existing
    value = {'provider': provider, 'generation': identity, 'bound_at': now}
    data['bindings'][session] = value
    circuit = data.get('providers', {}).get(provider, {})
    if circuit.get('probe_session') == session:
        circuit['probe_generation'] = identity
    data['last_provider'] = provider
    if data.get('next_provider') == provider:
        data.pop('next_provider', None)
    return value


def fail(data, session, provider, reason, identity, now):
    reason = NORMALIZED.get(reason, reason)
    if reason not in OUTAGES:
        return False
    if provider not in PROVIDERS or generation(identity.get('id')) != identity:
        raise ValueError('Invalid failure identity')
    binding = data.get('bindings', {}).get(session)
    if binding and (binding.get('generation') != identity or binding.get('provider') != provider):
        selected = data.get('selections', {}).get(session, {})
        if binding.get('generation') == identity or selected.get('provider') != provider or not (binding.get('bound_at', 0) < selected.get('selected_at', 0) <= now and selected['selected_at'] <= identity['started_at'] + 1):
            raise ValueError('Failure does not match bound generation or current selection')
    key = provider + ':' + session + ':' + identity['id'] + ':' + reason
    seen = data.setdefault('seen_failures', {})
    if key in seen:
        return False
    seen[key] = now
    if len(seen) > 200:
        for old in sorted(seen, key=seen.get)[:-200]:
            del seen[old]
    data.setdefault('providers', {})[provider] = {
        'reason': reason, 'last_failure': now, 'blocked_until': now + COOLDOWN,
        'probe_until': 0, 'source_session': session, 'generation': identity}
    return True


def success(data, session, identity, now):
    binding = data.get('bindings', {}).get(session, {})
    if binding.get('generation') != identity:
        return False
    provider = binding.get('provider')
    circuit = data.get('providers', {}).get(provider)
    if not circuit or binding.get('bound_at', 0) <= circuit.get('last_failure', 0):
        return False
    data['providers'].pop(provider, None)
    return True


def probe_identity(session):
    try:
        result = subprocess.run(['tmux', 'has-session', '-t', '=' + session], capture_output=True, text=True, timeout=5)
        if result.returncode == 1 and result.stderr.strip() == "can't find session: " + session:
            return False
        if result.returncode != 0:
            return None
        return observe_generation(session)
    except (OSError, subprocess.TimeoutExpired):
        return None


def allowed(data, session, provider, identity, now):
    validate(data)
    binding = data.get('bindings', {}).get(session)
    if binding and binding.get('generation') == identity and binding.get('provider') != provider:
        return False
    circuit = data.get('providers', {}).get(provider, {})
    if circuit.get('blocked_until', 0) > now:
        return False
    if not circuit.get('last_failure'):
        return True
    return circuit.get('probe_session') == session and (
        circuit.get('probe_generation') == identity if circuit.get('probe_generation') else circuit.get('probe_until', 0) > now)


def executable(provider):
    value = os.environ.get('FORGE_' + provider.upper() + '_BIN') or shutil.which(provider)
    if not value:
        return None
    path = Path(value).resolve()
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def read_routing(state):
    path = Path(state) / 'routing.json'
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError('Invalid routing state')
        return validate(value)
    except FileNotFoundError:
        if (Path(state)/'routing.initialized').exists():
            raise StateError('Initialized routing state is missing')
        return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--config', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('select', 'current', 'bind', 'failure', 'success', 'allowed'):
        command = sub.add_parser(action)
        command.add_argument('--session', required=True)
        if action in ('bind', 'failure', 'allowed'):
            command.add_argument('--provider', choices=PROVIDERS, required=True)
        if action in ('bind', 'failure', 'success', 'allowed'):
            command.add_argument('--generation', required=True)
        if action == 'failure':
            command.add_argument('--reason', required=True)
    sub.add_parser('status')
    for action in ('next', 'retry'):
        command = sub.add_parser(action)
        command.add_argument('--provider', choices=PROVIDERS, required=True)
    args = parser.parse_args()
    allowed_sessions = {r.short + '-' + role for r in load_repositories(args.config) if r.enabled
               for role in ('build', 'review', 'audit')}
    if hasattr(args, 'session') and args.session not in allowed_sessions:
        parser.error('Session is not an enabled configured Forge worker')
    try:
        now = time.time()
        if args.action == 'status':
            print(json.dumps(read_routing(args.state), sort_keys=True)); return 0
        if args.action == 'current':
            identity = observe_generation(args.session)
            binding = read_routing(args.state).get('bindings', {}).get(args.session, {})
            if not identity or binding.get('generation') != identity:
                return 1
            print(binding['provider']); return 0
        identity = None
        if hasattr(args, 'generation'):
            identity = generation(args.generation)
            if not identity or observe_generation(args.session) != identity:
                return 1
        if args.action == 'allowed':
            return 0 if allowed(read_routing(args.state), args.session, args.provider, identity, now) else 1
        with AtomicStore(args.state, 'routing').transaction() as data:
            validate(data)
            if args.action == 'select':
                available = {p for p in PROVIDERS if executable(p)}
                value = choose(data, available, args.session, now, probe_identity)
                if value['provider']:
                    print(value['provider']); return 0
                return 1
            if args.action == 'bind':
                bind(data, args.session, args.provider, identity, now)
            elif args.action == 'failure':
                fail(data, args.session, args.provider, args.reason, identity, now)
            elif args.action == 'success':
                success(data, args.session, identity, now)
            elif args.action == 'next':
                data['next_provider'] = args.provider
            elif args.action == 'retry':
                circuit = data.setdefault('providers', {}).get(args.provider, {})
                circuit.update(blocked_until=0, probe_until=0)
                circuit.pop('probe_generation', None)
                data['providers'][args.provider] = circuit
        return 0
    except (OSError, ValueError, StateError, TypeError, KeyError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
