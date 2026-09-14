"""Private, installation-owned credential copies. Configuration contains references only."""
import os
from pathlib import Path
import re
import stat
import uuid

from .settings import read_json, write_json


class Credentials:
    def __init__(self, root):
        self.root = Path(root) / 'credentials'

    def _private(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ValueError('Forge credential directory must be private')

    def put(self, reference, value, *, file=False):
        if not re.fullmatch(r'(?:FORGE_SETUP_[A-Z_]+|OPENAI_API_KEY)', reference):
            raise ValueError('Invalid credential reference')
        if not isinstance(value, str) or not 0 < len(value.encode()) <= 65536:
            raise ValueError('Invalid credential size')
        self._private()
        name = uuid.uuid4().hex
        fd = os.open(self.root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        index = read_json(self.root / 'index.json') if (self.root / 'index.json').exists() else {}
        index[reference] = {'name': name, 'file': file}
        write_json(self.root / 'index.json', index)

    def environment(self):
        env = dict(os.environ)
        if not self.root.exists():
            return env
        self._private()
        index = read_json(self.root / 'index.json') if (self.root / 'index.json').exists() else {}
        for reference, record in index.items():
            if (not re.fullmatch(r'(?:FORGE_SETUP_[A-Z_]+|OPENAI_API_KEY)', reference)
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
