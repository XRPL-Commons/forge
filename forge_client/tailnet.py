"""Resolve the local Tailscale CLI, including the official macOS app bundle."""
import os
from pathlib import Path
import shutil
import sys

from .contracts import SetupError


MACOS_EXECUTABLE = Path('/Applications/Tailscale.app/Contents/MacOS/Tailscale')


def executable():
    found = shutil.which('tailscale')
    if found:
        return found
    if sys.platform == 'darwin' and MACOS_EXECUTABLE.is_file() and os.access(MACOS_EXECUTABLE, os.X_OK):
        return str(MACOS_EXECUTABLE)
    raise SetupError('tailnet_cli_missing', 'Install Tailscale locally and connect this computer to the intended private tailnet.')


def environment():
    """Keep app-bundle invocation in CLI mode, including without a terminal.

    Tailscale's macOS launcher uses terminal environment markers to choose CLI
    mode. A fixed TERM works for JSON and does not inherit the operator's shell
    state or GitHub credentials.
    """
    return {**{key: value for key, value in os.environ.items()
               if key in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'}},
            'TERM': 'dumb'}
