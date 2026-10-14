"""Bounded private-file reads, strict JSON decoding and the maintenance lock."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

MAX_SOURCE = 512 * 1024 * 1024


class MigrationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise MigrationError('duplicate_json_key')
            result[key] = value
        return result
    def invalid(value):
        raise MigrationError('nonfinite_json_number')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError) as error:
        raise MigrationError('invalid_source_json') from error


def read_regular(path, *, limit=MAX_SOURCE):
    # Validate every ancestor as well: O_NOFOLLOW alone covers only the final path.
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise MigrationError('symlink_source')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise MigrationError('unsafe_source_file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        if len(raw) > limit or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise MigrationError('source_changed')
        return raw
    finally:
        os.close(fd)



@contextmanager
def operation_lock(root, name='.migration.lock'):
    path = Path(root)/name
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError('migration_busy') from None
        yield
    finally:
        os.close(fd)

