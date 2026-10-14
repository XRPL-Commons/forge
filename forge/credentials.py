"""Private, installation-owned credential copies. Configuration contains references only."""
import os
from pathlib import Path
import re
import stat
import uuid
import fcntl
import hashlib
import json
from contextlib import contextmanager

from .settings import read_json, write_json
from .harnesses.registry import manifests


class Credentials:
    def __init__(self, root):
        self.root = Path(root).absolute() / 'credentials'

    def _private(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ValueError('Forge credential directory must be private')

    @contextmanager
    def locked(self):
        self._private()
        fd = os.open(self.root / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    @staticmethod
    def valid_reference(reference):
        return (isinstance(reference, str) and
                (re.fullmatch(r'FORGE_SETUP_[A-Z_]+', reference)
                 or reference in {m['api_key_env'] for m in manifests().values()}
                 | {m['token_env'] for m in manifests().values() if m.get('token_env')}))

    def put(self, reference, value, *, file=False):
        with self.locked():
            self._put(reference, value, file=file)

    def _put(self, reference, value, *, file=False, login_generation=None):
        if not self.valid_reference(reference):
            raise ValueError('Invalid credential reference')
        if not isinstance(value, str) or not 0 < len(value.encode()) <= 65536:
            raise ValueError('Invalid credential size')
        self._private()
        name = uuid.uuid4().hex
        fd = os.open(self.root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        index = read_json(self.root / 'index.json') if (self.root / 'index.json').exists() else {}
        index[reference] = {'name': name, 'file': file,
                            'login_generation': login_generation or uuid.uuid4().hex}
        write_json(self.root / 'index.json', index)
        self._prune(index)

    def _prune(self, index):
        """Delete secrets no reference points to: replaced keys and rotated logins.

        A running job that mounted a replaced login keeps its open copy; its
        refresh then fails the CAS check instead of resurrecting it."""
        current = {record['name'] for record in index.values()}
        for path in self.root.iterdir():
            if re.fullmatch(r'[a-f0-9]{32}', path.name) and path.name not in current and path.is_file() and not path.is_symlink():
                path.unlink()

    def refresh(self, reference, expected, value):
        """CAS: an old runner cannot replace a newer login. Never update external files."""
        with self.locked():
            index = read_json(self.root / 'index.json') if (self.root / 'index.json').exists() else {}
            record = index.get(reference)
            if not record or not record['file']:
                return False
            path = Path(self.environment(only=[reference])[reference])
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
            self._put(reference, value, file=True, login_generation=self._login_generation(record))
            return True

    @staticmethod
    def _login_generation(record):
        # Older credential records acquire a stable opaque identity without
        # reading the secret or rotating its identity on the next token refresh.
        return record.get('login_generation') or uuid.uuid5(uuid.NAMESPACE_OID, record['name']).hex

    def login_generations(self, only=None):
        """Safe account identities: explicit reconnect changes them, refresh does not."""
        if not self.root.exists():
            return {}
        self._private()
        index = read_json(self.root / 'index.json') if (self.root / 'index.json').exists() else {}
        return {reference: self._login_generation(record) for reference, record in index.items()
                if only is None or reference in only}

    def publish_login_generations(self, store, config):
        """Publish only selected provider identities before a supervisor cycle."""
        from .store.configuration import Configuration
        providers = config.get('provider_pool') or [config]
        references = {reference for provider in providers
                      for reference in [provider.get('provider_auth_file_env'),
                                        *provider.get('provider_environment', [])] if reference}
        settings = {'generations': self.login_generations(only=references)}
        configuration = Configuration(store)
        with store.write() as db:
            row = db.execute("SELECT revision,settings_json FROM configuration WHERE domain='provider-logins.json' "
                             "AND subject='instance' AND active=1").fetchone()
            if row is None or json.loads(row['settings_json']) != settings:
                configuration.replace_in(db, 'provider-logins.json', settings,
                                         expected_revision=row['revision'] if row else 0)

    def environment(self, only=None):
        """The process environment plus stored credentials.

        Role App keys and terminal logins (`FORGE_SETUP_*`) come from the store only;
        the process environment never supplies them. `only` limits the stored
        credentials read to the named references."""
        env = {name: value for name, value in os.environ.items() if not name.startswith('FORGE_SETUP_')}
        env['FORGE_CREDENTIAL_ROOT'] = str(self.root.parent.resolve())
        if not self.root.exists():
            return env
        self._private()
        index = read_json(self.root / 'index.json') if (self.root / 'index.json').exists() else {}
        for reference, record in index.items():
            if only is not None and reference not in only:
                continue
            if (not self.valid_reference(reference)
                    or not re.fullmatch(r'[a-f0-9]{32}', record['name'])):
                raise ValueError('Invalid stored credential reference')
            path = self.root / record['name']
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
                    raise ValueError('Stored credentials must be private regular files')
                env[reference] = str(path) if record['file'] else stream.read()
        return env
