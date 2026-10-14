"""Shared guards for identity effects: the staging check, the effect lock and the audit row.

Operator sign-in lives in operator_auth, agent grants in client_connections.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import threading
import uuid

from .store.files import StateError
from .store.runtime import required
from .store.transitions import _json


def active_in(db):
    if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
        raise StateError('Staging imports cannot accept identity operations')


_locks = threading.local()


@contextmanager
def identity_effect_lock(root):
    """Serialize grant revocation and effects without a transaction spanning IO."""
    if hasattr(root, 'read_snapshot') and hasattr(root, 'root'):
        root = root.root
    else:
        root = required(root).root
    root = Path(root).absolute()
    held = getattr(_locks, 'held', None)
    if held is None:
        held = _locks.held = set()
    key = str(root)
    if key in held:
        yield
        return
    fd = os.open(root / '.identity-effects.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise StateError('Identity effect lock must be a private owned regular file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.add(key)
        yield
    finally:
        held.discard(key)
        os.close(fd)


def audit_in(db, clock, action, *, operator=None, connection=None, request_id=None, outcome='applied', details=None):
    from .mutation_audit import current
    context = current()
    if context:
        context.details['effect_recorded'] = True
    db.execute('INSERT INTO operator_audit(operator_id,session_id,connection_id,request_id,action,outcome,at,details_json) '
               'VALUES(?,?,?,?,?,?,?,?)', (str(operator['id']) if operator else context.operator if context else None,
                 context.session if context else None, connection or (context.connection if context else None),
                 request_id or (context.request_id if context else str(uuid.uuid4())), action, outcome, clock(),
                 _json({'kind': 'identity_effect', **(details or {})})))
