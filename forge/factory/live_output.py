"""A running worker's output for the operator UI, read while it runs.

Captured logs only exist once a job is collected, so until then the output
view reads the container's own tail through ContainerRunner.observe: the same
bounded, secret-scrubbed read the supervisor observes with, never a new engine
invocation. Reads are cached per job for a few seconds and serialized, so any
number of open panels polling every few seconds costs at most one engine call
per job per TTL.
"""
import json
import threading
import time

from .activity import cap, events

LINES = 200
TTL = 3.0
# Reasons the UI words; never engine output, which could carry anything.
UNAVAILABLE = ('not_started', 'container_missing', 'runner_unavailable')


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
        self.lock = threading.Lock()

    def read(self, key, observe):
        # One engine call at a time: a slow engine queues readers behind the
        # one in flight, and they all get its answer from the cache.
        with self.lock:
            hit = self.entries.get(key)
            now = self.clock()
            if hit and 0 <= now - hit[0] < self.ttl:
                return hit[1]
            value = observe()
            self.entries = {k: v for k, v in self.entries.items() if 0 <= now - v[0] < self.ttl}
            self.entries[key] = (self.clock(), value)
            return value


TAILS = LiveTails()


def observe(runner, job):
    """{'logs', 'at'} or {'unavailable': reason}; never raises for the engine."""
    from .runner import RunnerError
    try:
        observation = runner.observe(job, lines=LINES)
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
