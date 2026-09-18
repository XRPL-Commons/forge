"""Controller-owned, flock-serialized operational state. Never mount into workers."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


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


class AtomicStore:
    """One local controller trust domain, not a distributed GitHub claim lock.

    Corrupt or lost initialized state fails closed. Removing the entire state root
    requires operator/process reconciliation before deliberately initializing anew.
    """

    def __init__(self, root: str | Path, name: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / (name + ".json")
        self.marker = self.root / (name + ".initialized")
        self.lock_path = self.root / (name + ".lock")

    @contextmanager
    def transaction(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
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
            yield data
            atomic_json(self.path, data)
            if not self.marker.exists():
                atomic_json(self.marker, {"version": 1})


ACTIVE = {"starting", "running", "waiting_build", "cancelling"}
TERMINAL = {"completed", "cancelled", "blocked_auth", "blocked_quota",
            "blocked_permission", "failed_retryable", "failed_task", "failed_infrastructure"}
ROLES = {"builder", "reviewer", "auditor"}
# Equal round-robin rotation order. Grok Build is registered as an optional
# harness; only QUALIFIED_PROVIDERS may be dispatched by the legacy tmux
# supervisor until its real runner qualification passes (docs/provider-startup.md).
PROVIDER_ORDER = ("claude", "codex", "grok")
PROVIDERS = frozenset(PROVIDER_ORDER)
QUALIFIED_PROVIDERS = frozenset({"claude", "codex"})


def successor(provider: str | None, available=PROVIDERS) -> str | None:
    """Next provider in rotation after ``provider`` among ``available``.

    ``None`` or an unavailable provider starts the rotation. Returns ``None``
    only when nothing is available; a sole available provider succeeds itself.
    """
    if provider is not None and provider not in PROVIDERS:
        raise ValueError("unknown provider")
    order = [name for name in PROVIDER_ORDER if name in available]
    if not order:
        return None
    if provider not in order:
        return order[0]
    return order[(order.index(provider) + 1) % len(order)]


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    generation: int
    task_key: str
    role: str
    provider: str
    candidate: str
    state: str
    created_at: float
    updated_at: float
    deadline: float
    outcome: str | None = None
    provider_session_id: str | None = None
    process_group: int | None = None


class StateStore:
    def __init__(self, root: str | Path):
        self.store = AtomicStore(root, "attempts")

    def claim(self, task_key: str, role: str, provider: str, candidate: str,
              *, now: float | None = None, timeout: float = 1800,
              max_attempts: int = 3, max_provider_switches: int = 1) -> Attempt:
        if not task_key or not candidate or role not in ROLES or provider not in PROVIDERS:
            raise ValueError("task, candidate, role and provider required")
        if timeout <= 0 or max_attempts < 1 or max_provider_switches < 0:
            raise ValueError("invalid attempt limits")
        now = time.time() if now is None else now
        with self.store.transaction() as ledger:
            attempts = ledger.setdefault("attempts", {})
            related = [item for item in attempts.values() if item["task_key"] == task_key]
            if any(item["state"] in ACTIVE for item in related):
                raise StateError("task has an active or unverified stopped attempt")
            history = sorted((item for item in related if item["role"] == role
                              and item["candidate"] == candidate), key=lambda i: i["generation"])
            if len(history) >= max_attempts:
                raise StateError("persisted role/candidate attempt budget exhausted")
            if history and history[-1]["state"] in {"blocked_permission", "failed_task", "completed"}:
                raise StateError("candidate requires resolution/change, not another verdict")
            switches = sum(a["provider"] != b["provider"] for a, b in zip(history, history[1:]))
            if history and history[-1]["provider"] != provider:
                switches += 1
            if switches > max_provider_switches:
                raise StateError("persisted provider-switch budget exhausted")
            generation = max((item["generation"] for item in related), default=0) + 1
            attempt = Attempt(str(uuid.uuid4()), generation, task_key, role, provider,
                              candidate, "starting", now, now, now + timeout)
            attempts[attempt.attempt_id] = asdict(attempt)
        return attempt

    def read(self, attempt_id: str) -> Attempt:
        with self.store.transaction() as ledger:
            try:
                return Attempt(**ledger["attempts"][attempt_id])
            except KeyError as error:
                raise StateError("unknown attempt") from error

    def current(self, task_key: str) -> Attempt | None:
        with self.store.transaction() as ledger:
            items = [a for a in ledger.get("attempts", {}).values() if a["task_key"] == task_key]
            return Attempt(**max(items, key=lambda a: a["generation"])) if items else None

    def update(self, attempt_id: str, generation: int, *, state: str,
               now: float | None = None, provider_session_id: str | None = None,
               process_group: int | None = None) -> Attempt:
        if state not in ACTIVE:
            raise ValueError("use finish for terminal states")
        return self._change(attempt_id, generation, state, now, provider_session_id, process_group)

    def cancel(self, attempt_id: str, generation: int, *, now: float | None = None) -> Attempt:
        """Fence callbacks immediately; replacement remains blocked until finish(cancelled)."""
        return self._change(attempt_id, generation, "cancelling", now)

    def finish(self, attempt_id: str, generation: int, outcome: str,
               *, stopped: bool = False, now: float | None = None) -> Attempt:
        if outcome not in TERMINAL:
            raise ValueError("unknown outcome")
        if not stopped:
            raise StateError("trusted runner must verify process cleanup before finish")
        return self._change(attempt_id, generation, outcome, now)

    def _change(self, attempt_id, generation, state, now, session=None, group=None):
        now = time.time() if now is None else now
        with self.store.transaction() as ledger:
            items = ledger.get("attempts", {})
            item = items.get(attempt_id)
            if not item or item["generation"] != generation:
                raise StateError("unknown or stale attempt generation")
            newest = max(a["generation"] for a in items.values() if a["task_key"] == item["task_key"])
            if newest != generation or item["state"] not in ACTIVE:
                raise StateError("obsolete or terminal attempt cannot change")
            if item["state"] == "cancelling" and state not in {"cancelling", "cancelled"}:
                raise StateError("cancelled authority cannot report success")
            item.update(state=state, updated_at=now)
            if state in TERMINAL:
                item["outcome"] = state
            if session is not None:
                item["provider_session_id"] = session
            if group is not None:
                if group <= 1:
                    raise ValueError("invalid process group")
                item["process_group"] = group
            return Attempt(**item)
