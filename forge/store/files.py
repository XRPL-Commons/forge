"""Shared state error and crash-safe JSON file replacement."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


class StateError(RuntimeError):
    pass


def atomic_json(path: Path, value: object) -> None:
    """Persist replacement and directory entry; interruption leaves old or new JSON."""
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
