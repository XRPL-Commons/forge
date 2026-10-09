"""Reuse status views between polls until the state they are built from changes.

Each dashboard GET used to re-parse the whole ledger. A view is now rebuilt only
when a file under the runtime root changes, or after a short age so clock-based
fields (freshness, "merged today") stay current.
"""
import os
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

DIRECTORIES = ('', 'setup', 'state', 'state/factory', 'state/issues', 'state/github-access')
MAX_AGE = 10
MAX_ENTRIES = 32


def fingerprint(root):
    from .store.runtime import selected
    store=selected(root)
    if store is not None:
        # SQLite may live outside the installation tree. Include the WAL so a
        # committed request invalidates cached pages before the clock timeout.
        found=[]
        for path in (store.path,Path(str(store.path)+'-wal')):
            try:
                info=path.stat()
                found.append((str(path),info.st_ino,info.st_mtime_ns,info.st_size))
            except FileNotFoundError:found.append((str(path),None,0,0))
        return (store.instance_id,*found)
    found = []
    for name in DIRECTORIES:
        try:
            with os.scandir(Path(root) / name) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        found.append((name, entry.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            found.append((name, None, 0, 0))
    return tuple(sorted(found))


class ViewCache:
    def __init__(self, *, clock=time.monotonic, max_age=MAX_AGE):
        self.clock, self.max_age = clock, max_age
        self.entries = {}
        self.generation = secrets.token_hex(16)
        self.lock = threading.RLock()

    def get(self, root, key, build):
        with self.lock:
            key = (str(Path(root).resolve()), key)
            stamp = fingerprint(root)
            now = self.clock()
            hit = self.entries.get(key)
            if (hit and hit[0] == stamp and 0 <= now - hit[1] < self.max_age
                    and int(now // self.max_age) == int(hit[1] // self.max_age)):
                return hit[2]
            # One builder per process. Concurrent polls must not each parse the
            # same large ledger; nested section views reuse this reentrant lock.
            value = build()
            if len(self.entries) >= MAX_ENTRIES and key not in self.entries:
                self.entries.pop(min(self.entries, key=lambda k: self.entries[k][1]))
            self.entries[key] = (stamp, now, value)
            return value

    def response(self, root, key, build, token, validator=None):
        """Validate before projection/serialization, including after eviction.

        A short clock bucket bounds freshness of time-derived fields. Session
        capabilities and query arguments are part of the validator, and guards
        must run before calling this method. Weak tags allow proxy compression.
        """
        with self.lock:
            stamp = (self.generation, str(Path(root).resolve()), key, fingerprint(root),
                     int(self.clock() // self.max_age), token)
            etag = 'W/"' + hashlib.sha256(repr(stamp).encode()).hexdigest() + '"'
            if validator == etag:
                return etag, None
            body = self.get(root, ('response', etag),
                            lambda: json.dumps({**build(), 'ok': True, 'controls_token': token}).encode())
            return etag, body
