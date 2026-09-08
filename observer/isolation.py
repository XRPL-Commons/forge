"""Fail-closed observer deployment checks; no account or permission mutations."""
import grp
import os
from pathlib import Path
import stat


class IsolationError(ValueError):
    pass


def protected_path(path, *, directory=True):
    path = Path(path)
    if not path.is_absolute() or str(path) != str(path.resolve(strict=True)):
        raise IsolationError('observer path must be absolute and contain no symlinks')
    if directory and not path.is_dir():
        raise IsolationError('observer directory missing')
    current = path
    uid = os.geteuid()
    while True:
        info = current.stat()
        # A root-owned sticky /tmp cannot replace a protected, user-owned child.
        sticky_root = info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and bool(info.st_mode & stat.S_ISVTX)
        if info.st_uid not in {0, uid} or (info.st_mode & 0o022 and not sticky_root):
            raise IsolationError('observer path or ancestor is writable by another identity')
        if current == current.parent:
            break
        current = current.parent
    return path


def check_runtime(state_dir):
    """Require an explicitly separate account before baseline or scheduled scans.

    Worker identities are deployment configuration owned outside worker control.
    This cannot establish token scopes, ACLs, capabilities, or network policy;
    those remain separately verified rollout requirements.
    """
    declared = os.environ.get('FORGE_OBSERVER_WORKER_UIDS', '')
    try:
        worker_uids = {int(part) for part in declared.split(',') if part.strip()}
    except ValueError as exc:
        raise IsolationError('invalid declared worker UIDs') from exc
    if not worker_uids or os.geteuid() == 0 or os.geteuid() in worker_uids:
        raise IsolationError('observer requires a separate non-root identity and declared worker UIDs')
    forbidden_groups = set()
    for name in ('sudo', 'wheel', 'docker', 'lxd'):
        try:
            forbidden_groups.add(grp.getgrnam(name).gr_gid)
        except KeyError:
            pass
    if forbidden_groups & (set(os.getgroups()) | {os.getegid()}):
        raise IsolationError('observer may not belong to host-administration groups')
    code = Path(__file__).resolve()
    protected_path(code.parent)
    if code.stat().st_uid != 0 or code.parent.stat().st_uid != 0:
        raise IsolationError("observer code must be owned by root, not the model-client identity")
    protected_path(state_dir)
