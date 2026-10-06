"""Resumable import evidence and offline parity, never backend activation."""
import json
import time
from pathlib import Path

from ..db import Store
from .snapshot import Snapshot, MigrationError, decode, digest, encode

def importer_version():
    directory = Path(__file__).parent.parent.parent
    paths = sorted(path for path in directory.rglob('*') if path.suffix in ('.py','.sql'))
    return '1:' + digest(b''.join(path.relative_to(directory).as_posix().encode() + path.read_bytes() for path in paths))
CHUNK = 1024 * 1024


def records(value, path=(), depth=0):
    """Separate collection members; large records use deterministic bounded chunks."""
    if depth < 2 and isinstance(value, (dict, list)):
        keys = sorted(value) if isinstance(value, dict) else list(range(len(value)))
        yield path, {'container': 'dict' if isinstance(value, dict) else 'list', 'keys': keys}
        for key in keys:
            yield from records(value[key], (*path, key), depth + 1)
    else:
        yield path, {'value': value}


def document(snapshot, name):
    raw = snapshot.read(name)
    if name.endswith('.jsonl'):
        value = [decode(line) for line in raw.splitlines() if line.strip()]
        return 'jsonl', value
    return 'json', decode(raw)


class Importer:
    def __init__(self, snapshot, destination, *, batch=100, after_batch=None, create=True):
        self.source = snapshot if isinstance(snapshot, Snapshot) else Snapshot(snapshot)
        if type(batch) is not int or not 1 <= batch <= 1000:
            raise MigrationError('invalid_batch_size')
        self.batch, self.after_batch = batch, after_batch
        self.run_id = self.source.id
        version = importer_version()
        target = Path(destination).absolute()
        if target == self.source.root or target in self.source.root.parents or self.source.root in target.parents:
            raise MigrationError('overlapping_import_paths')
        candidate = Store(destination)
        if not create and not candidate.path.exists():
            raise MigrationError('import_missing')
        self.store = Store.open(destination) if candidate.path.exists() else Store.create(destination)
        with self.store.write() as db:
            existing = db.execute('SELECT * FROM import_runs').fetchall()
            if existing:
                if len(existing) != 1 or existing[0]['id'] != self.run_id or existing[0]['importer_version'] != version:
                    raise MigrationError('import_identity_conflict')
            else:
                # Never use a populated application database as an import destination.
                registries = {'schema_migrations','store_metadata','unit_states','unit_transitions','close_codes','gate_codes',
                              'job_roles','job_phases','part_states','exit_reasons','event_kinds','artifact_kinds','sqlite_sequence'}
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if row[0] not in registries]
                for table in tables:
                    quoted = '"' + table.replace('"','""') + '"'
                    if db.execute('SELECT 1 FROM '+quoted+' LIMIT 1').fetchone():
                        raise MigrationError('destination_not_empty')
                now = time.time()
                db.execute("INSERT INTO import_runs VALUES(?,?,?,?,'importing',NULL,?,?)", (self.run_id, version,
                           self.source.manifest['source_revision'], self.source.manifest['storage_generation'], now, now))
                db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
                db.execute("INSERT INTO store_metadata VALUES('source_manifest',?)", (self.run_id,))
                db.executemany('INSERT INTO import_sources VALUES(?,?,?,?,?)', [
                    (self.run_id, name, row['domain'], row['digest'], row['size']) for name, row in self.source.entries.items()])

    def _cursor(self, db, name):
        row = db.execute('SELECT imported_count FROM import_progress WHERE run_id=? AND table_name=?', (self.run_id, name)).fetchone()
        return row[0] if row else 0

    def archive(self):
        started = time.monotonic()
        for name in sorted(self.source.entries):
            fmt, value = document(self.source, name)
            canonical = digest(encode(value))
            items = list(records(value))
            with self.store.write() as db:
                db.execute('INSERT INTO import_documents VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING', (self.run_id, name, fmt, canonical, len(items)))
                row = db.execute('SELECT canonical_digest FROM import_documents WHERE run_id=? AND source_name=?', (self.run_id, name)).fetchone()
                if row[0] != canonical:
                    raise MigrationError('snapshot_changed')
                offset = self._cursor(db, name)
            chunk_rows = []
            for key, item in items:
                raw = encode(item)
                for sequence, start in enumerate(range(0, len(raw), CHUNK)):
                    chunk = raw[start:start + CHUNK]
                    chunk_rows.append((self.run_id, name, encode(key).decode(), sequence, digest(chunk), chunk))
            if offset > len(chunk_rows):
                raise MigrationError('invalid_import_cursor')
            for start in range(offset, len(chunk_rows), self.batch):
                batch = chunk_rows[start:start+self.batch]
                with self.store.write() as db:
                    if self._cursor(db, name) != start:
                        raise MigrationError('import_cursor_changed')
                    db.executemany('INSERT INTO import_records VALUES(?,?,?,?,?,?)', batch)
                    end = start + len(batch)
                    db.execute('INSERT INTO import_progress VALUES(?,?,?, ?,0,?,?) ON CONFLICT(run_id,table_name) DO UPDATE SET cursor=excluded.cursor,imported_count=excluded.imported_count,complete=excluded.complete,updated_at=excluded.updated_at',
                               (self.run_id, name, str(end), end, int(end == len(chunk_rows)), time.time()))
                if self.after_batch:
                    self.after_batch(name, end)
        return {'archive_seconds': round(time.monotonic() - started, 4)}

    def read(self, name):
        with self.store.read_snapshot() as db:
            rows = db.execute('SELECT record_key,sequence,digest,content FROM import_records WHERE run_id=? AND source_name=? ORDER BY record_key,sequence', (self.run_id, name)).fetchall()
        decoded = {}
        previous, chunks, expected = None, [], 0
        for row in rows:
            if row['record_key'] != previous:
                if previous is not None:
                    decoded[tuple(json.loads(previous))] = decode(b''.join(chunks))
                previous, chunks, expected = row['record_key'], [], 0
            if row['sequence'] != expected or digest(row['content']) != row['digest']:
                raise MigrationError('import_record_corrupt')
            chunks.append(row['content'])
            expected += 1
        if previous is not None:
            decoded[tuple(json.loads(previous))] = decode(b''.join(chunks))
        def build(path):
            if path not in decoded:
                raise MigrationError('import_record_missing')
            item = decoded.pop(path)
            if 'value' in item:
                return item['value']
            values = [(key, build((*path, key))) for key in item['keys']]
            return dict(values) if item['container'] == 'dict' else [v for _,v in values]
        value = build(())
        if decoded:
            raise MigrationError('extra_import_records')
        return value

    def validate(self):
        """Lossless source parity is separate from semantic/domain readiness."""
        started = time.monotonic()
        verified = 0
        for name in sorted(self.source.entries):
            _, source = document(self.source, name)  # Recheck sealed bytes on every validation.
            if encode(source) != encode(self.read(name)):
                raise MigrationError('source_parity_mismatch')
            with self.store.write() as db:
                db.execute('UPDATE import_progress SET validated_count=imported_count WHERE run_id=? AND table_name=? AND complete=1', (self.run_id, name))
            verified += 1
        with self.store.read_snapshot() as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                raise MigrationError('import_integrity_failed')
            counts = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in
                      ('import_records','repositories','issues','units','jobs','job_artifacts','events','budget_entries')}
            findings = [dict(row) for row in db.execute('SELECT code,blocking,count(*) AS count FROM import_findings WHERE run_id=? GROUP BY code,blocking', (self.run_id,))]
        from .parity import check
        semantic_checks = check(self)
        report = dict(status='staged', activation_allowed=False, source_manifest=self.run_id,
                      source_files=verified, source_bytes=sum(r['size'] for r in self.source.entries.values()),
                      source_parity_mismatches=0, normalized_checks=semantic_checks, public_view_parity='pending', rows=counts, findings=findings,
                      database_bytes=self.store.path.stat().st_size,
                      validation_seconds=round(time.monotonic()-started,4))
        with self.store.write() as db:
            # 'validated' is reserved for full domain/public-view parity. A byte
            # archive alone must never make this database eligible for selection.
            db.execute("UPDATE import_runs SET status='validating',validation_json=?,updated_at=? WHERE id=?", (encode(report).decode(),time.time(),self.run_id))
        return report

    def run(self):
        timing = self.archive()
        from .normalize import normalize
        normalize(self)
        return {**self.validate(), **timing}
