"""A running worker's output for the operator UI, read while it runs.

Captured logs only exist once a job is collected, so until then the output
view reads the container's own tail through ContainerRunner.observe: the same
bounded, secret-scrubbed observation path used by the supervisor. Reads are cached and coalesced per job for a few seconds, so any
number of open panels polling every few seconds costs at most one engine call
per job per TTL.
"""
from concurrent.futures import Future, TimeoutError as FutureTimeout
import json
import threading
import time

from .activity import cap, events

LINES = 200
TTL = 3.0
READ_TIMEOUT = 10.0


def heartbeat(line):
    """The entrypoint's `{"runner": "alive", "at": …}` line, else None."""
    if '"runner"' not in line[:32]:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get('runner') != 'alive':
        return None
    at = event.get('at')
    return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0


def latest(lines):
    """What the worker did last: (line, tool, at), from the newest recognised event."""
    for raw in reversed(lines):
        found = [entry for entry in events(raw) if entry[0] != 'alive']
        if not found:
            continue
        _, at, line = found[-1]
        try:
            event = json.loads(raw)
        except ValueError:
            event = {}
        tool = event.get('tool') if isinstance(event, dict) and event.get('type') == 'forge.activity' else None
        return {'text': cap(line), 'tool': cap(tool, 40) if isinstance(tool, str) and tool and tool != 'text' else None,
                'at': at if isinstance(at, (int, float)) and not isinstance(at, bool) else None}
    return None


def summarize(logs):
    """Heartbeats out of the text, folded into one time; the latest activity line."""
    lines, beat, kept = logs.splitlines(), None, []
    for line in lines:
        at = heartbeat(line)
        if at is None:
            kept.append(line)
        elif at:
            beat = max(beat or 0, at)
    return {'text': '\n'.join(kept), 'heartbeat_at': beat, 'activity': latest(lines[-LINES:])}


class LiveTails:
    def __init__(self, *, clock=time.monotonic, ttl=TTL):
        self.clock, self.ttl = clock, ttl
        self.entries = {}
        self.pending = {}
        self.lock = threading.Lock()

    def read(self, key, observe):
        # The lock owns only the cache and in-flight registry. Each job has one
        # reader; an unavailable engine for one job cannot queue other jobs.
        with self.lock:
            hit = self.entries.get(key)
            now = self.clock()
            if hit and 0 <= now - hit[0] < self.ttl:
                return hit[1]
            pending = self.pending.get(key)
            owner = pending is None
            if owner:
                pending = self.pending[key] = Future()
        if not owner:
            try:
                return pending.result(timeout=READ_TIMEOUT)
            except FutureTimeout:
                return {'unavailable': 'runner_unavailable'}
        try:
            value = observe()
            with self.lock:
                now = self.clock()
                self.entries = {k: v for k, v in self.entries.items() if 0 <= now - v[0] < self.ttl}
                self.entries[key] = (now, value)
            pending.set_result(value)
            return value
        except Exception as error:
            pending.set_exception(error)
            raise
        finally:
            with self.lock:
                del self.pending[key]


TAILS = LiveTails()


def observe(runner, job):
    """{'logs', 'at'} or {'unavailable': reason}; never raises for the engine."""
    from .runner import RunnerError
    try:
        observation = runner.observe(job, lines=LINES, timeout=READ_TIMEOUT)
    except (RunnerError, OSError, ValueError, KeyError, TypeError):
        return {'unavailable': 'runner_unavailable'}
    if observation.get('execution') is None:
        return {'unavailable': 'container_missing'}
    if observation.get('missing'):
        return {'unavailable': 'runner_unavailable'}
    return {'logs': observation.get('logs', ''), 'at': observation.get('at'),
            'running': bool(observation['execution'].get('running'))}


def runner_tail(root, *, tails=TAILS):
    """The status API's live reader: the job's own launch config and the
    installation's credentials, exactly what the supervisor scrubs with."""
    def read(job):
        def fresh():
            from ..credentials import Credentials
            from .runner import ContainerRunner
            try:
                environ = Credentials(root).environment()
            except (OSError, ValueError, KeyError):
                return {'unavailable': 'runner_unavailable'}
            return observe(ContainerRunner(job['runner_config'], environ=environ), job)
        return tails.read((str(root), job['id']), fresh)
    return read
