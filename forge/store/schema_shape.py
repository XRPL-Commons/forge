"""Compare two SQLite schemas by structure rather than by creation history.

A store that reached a schema through many ALTERs and a store created from one
script store different `sqlite_master` text for the same table. The shape keeps
what matters to the running code: every table's columns in order (type,
nullability, default, key), its foreign keys and indexes, its normalized DDL
(CHECK constraints and STRICT live only there), and the normalized text of
every index, view and trigger. Object order and whitespace are ignored. The
contents of the immutable registry tables are part of the shape, in row order,
because views order gate codes by rowid.
"""
import re
import sqlite3

# Runtime data, not schema. sqlite_sequence follows AUTOINCREMENT inserts.
NOT_SCHEMA = frozenset({'schema_migrations', 'store_metadata', 'sqlite_sequence'})


def normalize(sql):
    """DDL text without formatting: one space at most, none around punctuation, no identifier quotes."""
    text = re.sub(r'"([A-Za-z_][A-Za-z0-9_]*)"', r'\1', sql or '')
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r' ?([(),;=]) ?', r'\1', text)


def registries(db):
    """Tables a fresh store seeds and the schema forbids changing at runtime."""
    triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    return sorted(name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if name not in NOT_SCHEMA and {name + '_no_insert', name + '_no_update', name + '_no_delete'} <= triggers)


def _rows(db, sql, *args):
    return [tuple(row) for row in db.execute(sql, args)]


def shape(db, registry_tables=None):
    """A comparable description of every user object and the registry contents."""
    result = {}
    objects = _rows(db, "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")
    for kind, name, table, sql in objects:
        entry = {'table': table, 'sql': normalize(sql)}
        if kind == 'table':
            entry['columns'] = _rows(db, 'SELECT name,type,"notnull",dflt_value,pk,hidden FROM pragma_table_xinfo(?) ORDER BY cid', name)
            keys = {}
            for row in _rows(db, 'SELECT id,seq,"table","from","to",on_update,on_delete,"match" FROM pragma_foreign_key_list(?)', name):
                keys.setdefault(row[0], []).append(row[1:])
            entry['foreign_keys'] = sorted(tuple(sorted(group)) for group in keys.values())
            indexes = []
            for index, unique, origin, partial in _rows(db, 'SELECT name,"unique",origin,partial FROM pragma_index_list(?)', name):
                columns = _rows(db, 'SELECT seqno,cid,name,"desc",coll,"key" FROM pragma_index_xinfo(?) ORDER BY seqno', index)
                # Automatic index names depend only on the table; keep them.
                indexes.append((index, unique, origin, partial, tuple(columns)))
            entry['indexes'] = sorted(indexes)
        result[(kind, name)] = entry
    for name in registries(db) if registry_tables is None else registry_tables:
        result[('rows', name)] = _rows(db, f'SELECT * FROM "{name}" ORDER BY rowid')
    return result


def compare(db, script):
    """Differences between a live connection's schema and a new store built from script."""
    expected = baseline_shape(script)
    return differences(shape(db, [key[1] for key in expected if key[0] == 'rows']), expected)


def differences(actual, expected):
    """Readable differences, empty when both shapes describe the same schema."""
    found = []
    for key in sorted(set(actual) | set(expected)):
        label = ' '.join(key)
        if key not in expected:
            found.append(f'unexpected {label}')
        elif key not in actual:
            found.append(f'missing {label}')
        elif actual[key] != expected[key]:
            if key[0] == 'rows':
                found.append(f'different rows in {key[1]}')
                continue
            parts = sorted(part for part in expected[key] if actual[key].get(part) != expected[key][part])
            found.append(f'different {label}: {", ".join(parts)}')
    return found


def baseline_shape(script, registry_tables=None):
    """The shape of a new store created from one script, built in memory."""
    from .db import _execute_script
    from .remote_time import register
    db = sqlite3.connect(':memory:', isolation_level=None)
    try:
        register(db)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN')
        _execute_script(db, script)
        db.execute('COMMIT')
        return shape(db, registry_tables)
    finally:
        db.close()
