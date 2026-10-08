"""Controller-owned, flock-serialized operational state. Never mount into workers."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile


class StateError(RuntimeError):
    pass


def atomic_json(path: Path, value: object) -> None:
    from .legacy_writers import mutation
    with mutation(path):
        _atomic_json(path, value)


def _atomic_json(path: Path, value: object) -> None:
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


class AtomicStore:
    """One local controller trust domain, not a distributed GitHub claim lock.

    Corrupt or lost initialized state fails closed. Removing the entire state root
    requires operator/process reconciliation before deliberately initializing anew.
    """

    def __init__(self, root: str | Path, name: str):
        from .store.runtime import require_legacy
        require_legacy(root)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / (name + ".json")
        self.marker = self.root / (name + ".initialized")
        self.lock_path = self.root / (name + ".lock")

    def _load(self):
        if self.path.is_symlink() or self.marker.is_symlink():
            raise StateError("state files must not be symlinks")
        if not self.path.exists() and self.marker.exists():
            raise StateError("initialized state is missing; reconcile before recovery")
        try:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
        except (ValueError, OSError) as error:
            raise StateError("unreadable state; reconcile before recovery") from error
        if not isinstance(data, dict):
            raise StateError("invalid state object")
        return data

    def read(self):
        """An independent snapshot under a shared lock, without a durable write."""
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            return self._load()

    @contextmanager
    def transaction(self):
        from .legacy_writers import mutation
        # Include the read and caller mutation, not only the final rename.
        with mutation(self.path):
            yield from self._transaction()

    def _transaction(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._load()
            yield data
            atomic_json(self.path, data)
            if not self.marker.exists():
                atomic_json(self.marker, {"version": 1})
