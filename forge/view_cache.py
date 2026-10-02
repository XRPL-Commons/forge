"""Reuse status views between polls until the state they are built from changes.

Each dashboard GET used to re-parse the whole ledger. A view is now rebuilt only
when a file under the runtime root changes, or after a short age so clock-based
fields (freshness, "merged today") stay current.
"""
import os
import threading
import time
from pathlib import Path

DIRECTORIES = ('', 'setup', 'state', 'state/factory', 'state/issues', 'state/github-access')
MAX_AGE = 10
MAX_ENTRIES = 32


def fingerprint(root):
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
        self.lock = threading.Lock()

    def get(self, root, key, build):
        stamp = fingerprint(root)
        now = self.clock()
        with self.lock:
            hit = self.entries.get(key)
            if hit and hit[0] == stamp and now - hit[1] < self.max_age:
                return hit[2]
        value = build()
        with self.lock:
            if len(self.entries) >= MAX_ENTRIES and key not in self.entries:
                self.entries.pop(min(self.entries, key=lambda k: self.entries[k][1]))
            self.entries[key] = (stamp, now, value)
        return value
