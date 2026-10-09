"""Private SQLite storage primitives. Not selected by any runtime entry point.

Initialization is explicit and never repairs or replaces an incomplete store.
Connections belong to short context managers, not to the Store object.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid


class StoreError(RuntimeError):
    """A storage failure with a stable machine-readable code."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def qualified_sqlite(version=None):
    """Known upstream releases containing the WAL-reset corruption fix."""
    version = tuple(sqlite3.sqlite_version_info if version is None else version)
    return (version >= (3, 51, 3)
            or version[:2] == (3, 44) and version >= (3, 44, 6)
            or version[:2] == (3, 50) and version >= (3, 50, 7))


def _qualify():
    if not qualified_sqlite():
        raise StoreError("sqlite_unqualified", "The linked SQLite library lacks a qualified WAL-reset fix")


def _private(path, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise StoreError("store_missing", "A required private store path is missing") from error
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    wanted = 0o700 if directory else 0o600
    if (not kind(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != wanted
            or not directory and info.st_nlink != 1):
        raise StoreError("unsafe_permissions", "Store paths must be owned private directories or regular files")


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _new_file(path, content=b""):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        # No process-global umask changes; explicit modes are checked afterwards.
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _migrations():
    result = []
    for path in sorted((Path(__file__).parent / "schema").glob("[0-9]*_*.sql")):
        version = int(path.name.split("_", 1)[0])
        payload = path.read_bytes()
        if version != len(result) + 1:
            raise StoreError("schema_invalid", "Packaged schema migrations are not sequential")
        result.append((version, hashlib.sha256(payload).hexdigest(), payload.decode("utf-8")))
    if not result:
        raise StoreError("schema_invalid", "No schema migrations are installed")
    return result


def _execute_script(connection, script):
    # sqlite3.executescript implicitly commits: parse complete statements instead.
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip() and not all(line.lstrip().startswith("--") for line in statement.splitlines() if line.strip()):
        raise StoreError("schema_invalid", "A schema migration has an incomplete statement")


def _database_error(error):
    code = getattr(error, "sqlite_errorcode", 0) & 0xff
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return StoreError("store_busy", "The store is busy; retry the operation with the same idempotency key")
    if code == sqlite3.SQLITE_FULL:
        return StoreError("store_full", "The store could not commit because storage is full")
    if code == sqlite3.SQLITE_IOERR:
        return StoreError("store_io", "The store encountered an I/O error")
    return StoreError("store_invalid", "The private store could not be validated")


class Store:
    """Handle to an initialized private store; creating a handle opens no connection."""

    busy_timeout_ms = 1000

    def __init__(self, private_root):
        self.root = Path(private_root).absolute()
        self.directory = self.root / "state"
        self.path = self.directory / "forge.sqlite3"
        self.anchor_path = self.directory / "instance.json"
        self.instance_id = None
        self._reader = ContextVar("forge_store_reader", default=None)

    @classmethod
    def create(cls, private_root, instance_id=None):
        _qualify()
        store = cls(private_root)
        instance_id = str(uuid.uuid4()) if instance_id is None else instance_id
        if not isinstance(instance_id, str) or not instance_id or len(instance_id) > 200:
            raise ValueError("instance_id must be a nonempty string of at most 200 characters")
        if not store.root.exists():
            store.root.mkdir(mode=0o700)
            os.chmod(store.root, 0o700)
            _fsync_directory(store.root.parent)
        _private(store.root, directory=True)
        try:
            store.directory.mkdir(mode=0o700)
            os.chmod(store.directory, 0o700)
            _fsync_directory(store.root)
        except FileExistsError:
            _private(store.directory, directory=True)
        # Never silently reset data after an interrupted initializer or lost file.
        if any(store.directory.iterdir()):
            raise StoreError("store_exists", "The state directory is not empty; explicit recovery is required")
        pending = store.directory / "forge.sqlite3.initializing"
        try:
            _new_file(pending)
        except FileExistsError as error:
            raise StoreError("store_exists", "Another initializer owns this store") from error
        connection = None
        try:
            connection = store._connect_path(pending)
            connection.execute("BEGIN IMMEDIATE")
            migrations = _migrations()
            for version, checksum, script in migrations:
                _execute_script(connection, script)
                connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, checksum, time.time()))
            connection.executemany("INSERT INTO store_metadata(key, value) VALUES (?, ?)", (
                ("instance_id", instance_id), ("schema_version", str(migrations[-1][0])),
            ))
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StoreError("store_invalid", "Initialized database failed integrity validation")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StoreError("store_invalid", "Initialized database failed foreign-key validation")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0]:
                raise StoreError("store_busy", "Initialization checkpoint could not finish")
            connection.close()
            connection = None
            # A crash between publication and the anchor leaves an incomplete pair.
            # open() refuses it, and create() never silently starts a second store.
            os.link(pending, store.path)
            pending.unlink()
            _fsync_directory(store.directory)
            anchor_pending = store.directory / ".instance.pending"
            _new_file(anchor_pending, json.dumps({"format": 1, "instance_id": instance_id}).encode("utf-8"))
            os.link(anchor_pending, store.anchor_path)
            anchor_pending.unlink()
            _fsync_directory(store.directory)
        except sqlite3.DatabaseError as error:
            raise _database_error(error) from error
        finally:
            if connection is not None:
                connection.close()
        return cls.open(private_root)

    def _paths(self):
        _private(self.root, directory=True)
        _private(self.directory, directory=True)
        _private(self.path)
        _private(self.anchor_path)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.path) + suffix)
            if os.path.lexists(sidecar):
                _private(sidecar)

    def _anchor(self):
        try:
            data = json.loads(self.anchor_path.read_text())
        except (ValueError, OSError) as error:
            raise StoreError("store_invalid", "The instance anchor is unreadable") from error
        if (not isinstance(data, dict) or set(data) != {"format", "instance_id"}
                or data["format"] != 1 or not isinstance(data["instance_id"], str) or not data["instance_id"]):
            raise StoreError("store_invalid", "The instance anchor is invalid")
        return data["instance_id"]

    def _connect_path(self, path, read_only=False):
        _private(path)
        mode = "ro" if read_only else "rw"
        connection = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, isolation_level=None,
                                     timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        from .remote_time import register
        register(connection)
        try:
            connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise StoreError("store_invalid", "WAL could not be enabled")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            if (connection.execute("PRAGMA synchronous").fetchone()[0] != 2
                    or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                    or connection.execute("PRAGMA trusted_schema").fetchone()[0] != 0):
                raise StoreError("store_invalid", "Required SQLite settings could not be enabled")
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(path) + suffix)
                if os.path.lexists(sidecar):
                    _private(sidecar)
            return connection
        except BaseException:
            connection.close()
            raise

    def _validate(self, connection):
        migrations = _migrations()
        rows = connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
        if rows and rows[-1][0] > migrations[-1][0]:
            raise StoreError("schema_newer", "The store schema is newer than this binary supports")
        if len(rows) != len(migrations):
            raise StoreError("schema_incomplete", "The store requires an explicit schema migration or recovery")
        if [(row[0], row[1]) for row in rows] != [(version, checksum) for version, checksum, _ in migrations]:
            raise StoreError("schema_checksum", "Applied schema migration checksums do not match this binary")
        metadata = dict(connection.execute("SELECT key, value FROM store_metadata"))
        if metadata.get("schema_version") != str(migrations[-1][0]):
            raise StoreError("schema_incomplete", "Schema metadata does not match applied migrations")
        expected = self._anchor()
        if metadata.get("instance_id") != expected or self.instance_id not in (None, expected):
            raise StoreError("instance_mismatch", "Database and instance anchor do not match")
        self.instance_id = expected

    @classmethod
    def open(cls, private_root):
        _qualify()
        store = cls(private_root)
        with store._connection() as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StoreError("store_invalid", "The store failed integrity validation")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StoreError("store_invalid", "The store failed foreign-key validation")
        return store

    @contextmanager
    def _connection(self, read_only=False):
        _qualify()
        self._paths()
        connection = None
        try:
            connection = self._connect_path(self.path, read_only=read_only)
            self._validate(connection)
            yield connection
        except sqlite3.OperationalError as error:
            raise _database_error(error) from error
        except sqlite3.DatabaseError as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise _database_error(error) from error
            raise
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def read_session(self):
        """Share one snapshot across the nested readers of a public projection.

        The session is explicit, scoped to this Store and execution context, and
        closes before returning the response. It never caches state between reads.
        """
        current = self._reader.get()
        if current is not None:
            yield current
            return
        with self.read_snapshot() as connection:
            token = self._reader.set(connection)
            try:
                yield connection
            finally:
                self._reader.reset(token)

    @contextmanager
    def read_snapshot(self):
        current = self._reader.get()
        if current is not None:
            yield current
            return
        with self._connection(read_only=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN DEFERRED")
            # Establish the snapshot before yielding, even if callers delay reads.
            connection.execute("SELECT value FROM store_metadata WHERE key='instance_id'").fetchone()
            try:
                yield connection
            finally:
                connection.rollback()

    @contextmanager
    def write(self):
        if self._reader.get() is not None:
            raise StoreError("read_session_write", "A public read session cannot perform mutations")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def backup(self, destination):
        """Create a private, verified SQLite backup without replacing existing files."""
        destination = Path(destination).absolute()
        _private(destination.parent, directory=True)
        if any(os.path.lexists(Path(str(destination) + suffix)) for suffix in ("", "-wal", "-shm", "-journal")):
            raise StoreError("backup_exists", "Backup destination or sidecars already exist")
        pending = destination.with_name(destination.name + ".pending-" + uuid.uuid4().hex)
        try:
            _new_file(pending)
        except FileExistsError as error:
            raise StoreError("backup_exists", "Backup destination already exists") from error
        target = None
        try:
            target = sqlite3.connect(pending.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            target.execute("PRAGMA synchronous=FULL")
            with self.read_snapshot() as source:
                source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StoreError("backup_invalid", "Backup integrity validation failed")
            if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StoreError("backup_invalid", "Backup foreign-key validation failed")
            self._validate(target)
            target.close()
            target = None
            _private(pending)
            descriptor = os.open(pending, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.link(pending, destination)
            pending.unlink()
            _fsync_directory(destination.parent)
            return destination
        except BaseException:
            if target is not None:
                target.close()
            pending.unlink(missing_ok=True)
            raise
