"""Host admission for container workers; no swap is treated as available RAM."""
from contextlib import contextmanager
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
import time

GIB = 1024 ** 3
HOST_RESERVE = 2 * GIB


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


def memory_bytes(value):
    return int(value[:-1]) * (1024 ** (2 if value[-1] == 'm' else 3))


def observe(requested, containers, *, reserve=HOST_RESERVE, read_text=None):
    """Reserve the full limit, including created/orphaned workers, not just usage.

    MemAvailable also accounts for non-Forge processes. The aggregate budget
    prevents sequential launches from spending the same still-unused RAM twice.
    External host processes can still allocate memory after this observation.
    """
    read_text = read_text or (lambda path: Path(path).read_text())
    decision = {'allowed': False, 'reason': 'telemetry_unavailable', 'at': time.time()}
    try:
        memory = parse_meminfo(read_text('/proc/meminfo'))
        pressure = parse_pressure(read_text('/proc/pressure/memory'))
        if type(requested) is not int or requested <= 0:
            raise ValueError('Invalid worker memory budget')
        reserved = 0
        for container in containers:
            limit = container['HostConfig']['Memory']
            if type(limit) is not int or limit <= 0:
                raise ValueError('Unbounded worker memory budget')
            # Retain reservations for stopped workers until verified removal.
            reserved += limit
        decision.update(requested_bytes=requested, reserved_bytes=reserved,
                        available_bytes=memory['MemAvailable'], total_bytes=memory['MemTotal'],
                        host_reserve_bytes=reserve)
        if reserved + requested + reserve > memory['MemTotal']:
            decision['reason'] = 'memory_budget'
        elif memory['MemAvailable'] < requested + reserve:
            decision['reason'] = 'memory_headroom'
        elif any(pressure[kind][window] > threshold
                 for kind, threshold in (('some', 1.0), ('full', 0.1))
                 for window in ('avg10', 'avg60')):
            decision['reason'] = 'memory_pressure'
        else:
            decision.update(allowed=True, reason='capacity_available')
    except (OSError, ValueError, TypeError, KeyError):
        decision['reason'] = 'telemetry_unavailable'
    return decision


@contextmanager
def launch_lock(engine_socket):
    """Serialize check + start across factory, setup probes and runtime roots."""
    directory = Path(tempfile.gettempdir()) / ('forge-admission-' + str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError('Private admission lock directory is unavailable')
    name = hashlib.sha256(str(Path(engine_socket).resolve()).encode()).hexdigest() + '.lock'
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield
