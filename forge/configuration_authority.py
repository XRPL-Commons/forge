"""One boundary for configuration scope, storage ownership and revisions.

Callers choose ``live``, ``setup`` or the operator's ``active`` scope. Path
recognition is only a compatibility adapter for pre-cutover callers. A selected
SQLite installation never reads configuration or phase from the old JSON tree.
"""
from pathlib import Path

from .store.configuration import Configuration, ConfigurationConflict
from .store.runtime import instance_root, selected, control_enabled

RUNTIME_DOMAINS = ('host-runtime.json', 'setup/runtime.json', 'runtime.json')
CONFIGURATION_DOMAINS = frozenset((*RUNTIME_DOMAINS, 'factory.json', 'setup/factory.json', 'host.json'))


class ConfigurationAuthority:
    def __init__(self, root, *, scope=None, database=None):
        supplied = Path(root)
        self.root = instance_root(root)
        self.database = database if database is not None else selected(self.root)
        scope = scope or ('setup' if supplied.name == 'setup' else 'live')
        if scope == 'active':
            scope = 'live' if self.active() else 'setup'
        if scope not in ('live', 'setup'):
            raise ValueError('Configuration scope must be live, setup or active')
        self.scope = scope
        self.path = self.root / 'setup' if scope == 'setup' else self.root

    def active(self):
        if self.database is not None:
            with self.database.read_snapshot() as db:
                row = db.execute("SELECT enabled FROM runtime_controls WHERE name='factory_active'").fetchone()
                return bool(row and row[0])
        return control_enabled(self.root, 'factory_active')

    def phase(self):
        if self.database is not None:
            with self.database.read_snapshot() as db:
                row = db.execute("SELECT phase FROM setup_instances WHERE scope='live'").fetchone()
                return row[0] if row else None
        from .settings import read_json
        path = self.root / 'state/onboarding/setup.json'
        return read_json(path).get('phase') if path.is_file() else None

    def domain(self, name):
        if name not in ('factory.json', 'runtime.json'):
            raise ValueError('Unknown scoped configuration document')
        return 'setup/' + name if self.scope == 'setup' else name

    @staticmethod
    def _check_domain(domain):
        if not isinstance(domain, str) or domain not in CONFIGURATION_DOMAINS:
            raise ValueError('Unknown configuration document')

    def snapshot(self, domain):
        self._check_domain(domain)
        if self.database is not None:
            return Configuration(self.database).get(domain)
        from .settings import read_json, digest
        path = self.root / domain
        if not path.exists():
            return None
        value = read_json(path)
        return {'settings': value, 'revision': digest(value), 'credential_ref': None}

    def runtime(self, *, host_default=False):
        row = self.snapshot(self.domain('runtime.json'))
        if row is None and host_default:
            row = self.snapshot('host-runtime.json')
        return row

    def replace(self, domain, value, *, expected_revision, credential_ref=None):
        self._check_domain(domain)
        if self.database is not None:
            return Configuration(self.database).replace(domain, value, expected_revision=expected_revision,
                                                        credential_ref=credential_ref)
        from .settings import write_json, digest
        current = self.snapshot(domain)
        if expected_revision != (current['revision'] if current else 0):
            raise ConfigurationConflict('Configuration changed; reload before saving')
        write_json(self.root / domain, value)
        return digest(value)
