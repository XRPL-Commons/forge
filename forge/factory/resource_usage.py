"""Bounded host observations of the exact worker container; never worker claims."""
from datetime import datetime
import http.client
import json
import math
import re
import socket
import time

MAX_STATS_BYTES = 65536
STATS_TIMEOUT = 2.0


def engine_time(value):
    if not isinstance(value, str) or value.startswith('0001-'):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        result = parsed.timestamp() if parsed.tzinfo is not None else None
        return result if result is not None and math.isfinite(result) else None
    except (ValueError, OverflowError):
        return None


class _DeadlineSocket(socket.socket):
    """HTTP buffered reads share one overall deadline, including headers."""
    def __init__(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Resource observation timed out')
        super().__init__(socket.AF_UNIX, socket.SOCK_STREAM)
        self.deadline = deadline
        self.settimeout(remaining)

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Resource observation timed out')
        self.settimeout(remaining)

    def recv_into(self, *args):
        self._remaining()
        return super().recv_into(*args)

    def sendall(self, *args):
        self._remaining()
        return super().sendall(*args)


class _EngineConnection(http.client.HTTPConnection):
    def __init__(self, path, deadline):
        super().__init__('localhost')
        self.path, self.deadline = path, deadline

    def connect(self):
        self.sock = _DeadlineSocket(self.deadline)
        self.sock.connect(self.path)


def stats(socket_path, container_id, *, deadline=None):
    """One raw Engine sample from the same explicit Unix socket as runner calls."""
    checked_at = time.time()
    def unavailable(reason):
        return dict(checked_at=checked_at, status='unavailable', reason=reason, sample=None)
    if not re.fullmatch('[0-9a-f]{64}', container_id):
        return unavailable('container_identity_unavailable')
    end = min(deadline, time.monotonic()+STATS_TIMEOUT) if deadline is not None else time.monotonic()+STATS_TIMEOUT
    connection = _EngineConnection(socket_path, end)
    try:
        connection.request('GET', '/version', headers={'Connection':'close'})
        version_response = connection.getresponse()
        raw = version_response.read(MAX_STATS_BYTES+1)
        if version_response.status != 200 or len(raw)>MAX_STATS_BYTES:
            return unavailable('engine_api_unsupported')
        version = json.loads(raw)
        maximum, minimum = version.get('ApiVersion'), version.get('MinAPIVersion')
        if not all(isinstance(v,str) and re.fullmatch(r'1\.[0-9]+',v) for v in (maximum,minimum)):
            return unavailable('engine_api_unsupported')
        selected = min(int(maximum.split('.')[1]),51)
        if selected < max(int(minimum.split('.')[1]),41):
            return unavailable('engine_api_unsupported')
        connection.request('GET', f'/v1.{selected}/containers/{container_id}/stats?stream=false&one-shot=true', headers={'Connection':'close'})
        response = connection.getresponse()
        if response.status != 200:
            return unavailable('engine_stats_unavailable')
        raw = response.read(MAX_STATS_BYTES+1)
        if len(raw)>MAX_STATS_BYTES:
            return unavailable('engine_stats_invalid')
        value = json.loads(raw)
        # Moby's one-shot success response can omit ID; identity is pinned
        # by this exact full-ID request after the runner's ownership inspection.
        if value.get('id') not in (None, '', container_id):
            return unavailable('engine_stats_invalid')
        sampled_at = engine_time(value.get('read'))
        cpu = value.get('cpu_stats', {}).get('cpu_usage', {}).get('total_usage')
        memory = value.get('memory_stats', {}).get('usage')
        cpu = cpu/1_000_000_000 if type(cpu) is int and 0 <= cpu <= 2**64-1 else None
        memory = memory if type(memory) is int and 0 <= memory < 2**63 else None
        if sampled_at is None or cpu is None and memory is None:
            return unavailable('engine_stats_unavailable')
        return dict(checked_at=checked_at, status='available' if cpu is not None and memory is not None else 'partial',
            reason=None if cpu is not None and memory is not None else 'counter_unavailable',
            sample=dict(sampled_at=sampled_at, cpu_seconds=cpu, memory_bytes=memory, provenance='docker_engine_stats'))
    except (OSError, http.client.HTTPException, ValueError, TypeError, AttributeError):
        return unavailable('engine_stats_unavailable')
    finally:
        connection.close()
