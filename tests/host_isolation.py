"""Fail tests if an injected cleanup runner accidentally falls back to host tmux."""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


def deny_host_tmux(test):
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    root = Path(directory.name)
    marker = root / 'attempted'
    binary = root / 'tmux'
    binary.write_text('#!/bin/sh\nprintf attempted > "' + str(marker) + '"\nexit 97\n')
    binary.chmod(0o755)
    environment = patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ.get('PATH','')})
    environment.start()
    test.addCleanup(environment.stop)
    test.addCleanup(lambda: test.assertFalse(marker.exists(), 'Test runner escaped its injected tmux boundary'))
