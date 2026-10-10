"""One server-owned idle read handle; transactions remain request-owned."""
import sqlite3
import threading
import weakref


class ReadCursor(sqlite3.Cursor):
    def _check(self):
        if self.generation != self.connection.generation:
            raise sqlite3.ProgrammingError('Read cursor escaped its snapshot')
        self.connection.check_owner()

    def execute(self, *args):
        self._check()
        return super().execute(*args)

    def executemany(self, *args):
        self._check()
        return super().executemany(*args)

    def executescript(self, *args):
        self._check()
        return super().executescript(*args)

    def fetchone(self):
        self._check()
        return super().fetchone()

    def fetchmany(self, *args):
        self._check()
        return super().fetchmany(*args)

    def fetchall(self):
        self._check()
        return super().fetchall()

    def __next__(self):
        self._check()
        return super().__next__()


class ReadConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = threading.get_ident()
        self.generation = 0
        self.cursors = weakref.WeakSet()

    def check_owner(self):
        if self.owner != threading.get_ident():
            raise sqlite3.ProgrammingError('Read connection is not owned by this request thread')

    def cursor(self, factory=None):
        self.check_owner()
        if factory is not None:
            raise sqlite3.ProgrammingError('Custom cursors are not supported by a reusable read handle')
        cursor = super().cursor(ReadCursor)
        cursor.generation = self.generation
        self.cursors.add(cursor)
        return cursor

    def execute(self, *args):
        return self.cursor().execute(*args)

    def executemany(self, *args):
        return self.cursor().executemany(*args)

    def executescript(self, *args):
        return self.cursor().executescript(*args)

    def commit(self):
        self.check_owner()
        return super().commit()

    def close(self):
        if self.owner is not None:
            self.check_owner()
        return super().close()

    def rollback(self):
        self.check_owner()
        # Explicitly finalize partially consumed results before ending a snapshot.
        for cursor in tuple(self.cursors):
            cursor.close()
        self.cursors.clear()
        self.generation += 1
        super().rollback()


class IdleReadHandle:
    """Exclusive request checkout, no waiting for SQL or response completion.

    SQLite serialized mode is required for transfer across handler threads. The
    lock owns only the idle slot; each active request owns its physical handle.
    """
    def __init__(self):
        if sqlite3.threadsafety != 3:
            raise RuntimeError('Reusable HTTP reads require serialized SQLite')
        self.lock = threading.Lock()
        self.store = None
        self.idle = None
        self.closed = False
        self.generation = 0

    def take(self, store):
        retired = None
        with self.lock:
            if self.closed:
                raise RuntimeError('HTTP read handles are closed')
            if self.store is not store:
                retired = self.idle
                self.idle = None
                self.store = store
                self.generation += 1
            generation = self.generation
            lease, self.idle = self.idle, None
        if retired is not None:
            retired['connection'].close()
        if lease is None:
            lease = dict(connection=None, identity=None)
        else:
            try:
                changed = lease['identity'] != self.identity(store)
            except BaseException:
                lease['connection'].close()
                raise
            if changed:
                lease['connection'].close()
                lease = dict(connection=None, identity=None)
        if lease['connection'] is not None:
            lease['connection'].owner = threading.get_ident()
        lease.update(thread=threading.get_ident(), busy=False, failed=False, transferable=True, generation=generation)
        return lease

    @staticmethod
    def identity(store):
        info = store.path.stat()
        return (info.st_dev, info.st_ino)

    def release(self, store, lease):
        connection = lease['connection']
        if connection is None:
            return
        try:
            connection.rollback()
            if connection.in_transaction:
                raise RuntimeError('Read transaction did not finish')
        except BaseException:
            connection.close()
            raise
        connection.owner = None
        with self.lock:
            if not self.closed and self.store is store and lease['generation'] == self.generation and self.idle is None and not lease['failed']:
                self.idle = lease
                return
        connection.close()

    def close(self):
        with self.lock:
            self.closed = True
            idle, self.idle = self.idle, None
        if idle is not None:
            idle['connection'].close()
