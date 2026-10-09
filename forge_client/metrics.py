"""Installation timing uses recorded events, never inferred file timestamps."""
import json
import math
import os
from pathlib import Path
import re
import sys

from .contracts import require


def client_installation(root, *, prefix=None):
    name = Path(prefix or sys.prefix).name
    match = re.fullmatch(r'([a-f0-9]{40})-([a-f0-9]{32})', name)
    if not match:
        return None  # Source/pipx installs without this receipt have unknown CLI start time.
    path = Path(root) / ('cli-install-' + match[2] + '.json')
    if not path.is_file() or path.is_symlink(): return None
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096: return None
    try:
        value = json.loads(path.read_text())
        if (value.get('version') != 1 or value.get('id') != match[2] or value.get('revision') != match[1]
                or value.get('status') != 'installed'
                or type(value.get('started_at')) not in (float, int)
                or type(value.get('installed_at')) not in (float, int)
                or not math.isfinite(value['installed_at'])
                or not 0 < value['started_at'] <= value['installed_at']): return None
        return {key: value[key] for key in ('id', 'revision', 'started_at', 'installed_at')}
    except (OSError, ValueError, TypeError):
        return None


def first_merge_timing(state, merged_at):
    setup_start = state['started_at']
    installation = state.get('cli_installation')
    cli_start = installation['started_at'] if installation else None
    require(math.isfinite(setup_start) and math.isfinite(merged_at) and 0 < setup_start <= merged_at
            and (installation is None or 0 < cli_start <= installation['installed_at'] <= setup_start),
            'timing_unavailable', 'Recorded installation, setup and merge times are not in chronological order.')
    return {'cli_installation_started_at': cli_start, 'setup_started_at': setup_start, 'merged_at': merged_at,
            'time_to_first_merge_seconds': merged_at - cli_start if cli_start is not None else None,
            'setup_to_first_merge_seconds': merged_at - setup_start,
            'timing_source': 'cli_install_receipt' if installation else 'setup_only_cli_start_unknown'}
