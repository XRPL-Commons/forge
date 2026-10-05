"""Disposable design check. This does not connect to a Forge runtime."""
from pathlib import Path
import sqlite3
import sys

source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name('state-and-units-of-work.sql')
db = sqlite3.connect(':memory:')
db.executescript(source.read_text())
db.execute("INSERT INTO repositories VALUES(1,'example/repo')")
db.execute("INSERT INTO repositories VALUES(2,'example/other')")
for key, repo, number in [('1:1', 1, 1), ('1:2', 1, 2), ('1:3', 1, 3), ('2:1', 2, 1)]:
    revision = 'r' + key
    db.execute("INSERT INTO issues VALUES(?,?,?,'open',?,1,1)", (key, repo, number, revision))
    db.execute("INSERT INTO issue_revisions VALUES(?,?,'Title','Body','hash','requirements','{}',1)", (revision, key))
for unit in ('u1', 'u2'):
    db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,1,'main','queued',1,1)", (unit,))
db.execute("INSERT INTO unit_issues VALUES('m1','u1','1:1',1,'main','r1:1','pending',1,NULL,NULL)")
db.execute("INSERT INTO unit_issues VALUES('m2','u1','1:2',1,'main','r1:2','pending',1,NULL,NULL)")
db.execute("INSERT INTO unit_issues VALUES('m3','u2','1:3',1,'main','r1:3','pending',1,NULL,NULL)")
for gate, member in [('g1', 'm1'), ('g2', 'm2')]:
    db.execute("INSERT INTO unit_gates(id,unit_id,member_id,subject_key,code,created_at) VALUES(?,'u1',?,?,'recovery_pending',1)", (gate, member, 'member:' + member))
db.execute("INSERT INTO attempts VALUES('a1','u1','m1',1,1,'{}',1,1)")
db.execute("INSERT INTO jobs VALUES('j1','u1','a1','m1',NULL,'build','reserved',NULL,0,0,1)")
db.execute("INSERT INTO job_leases VALUES('build-1','j1','fence1',1)")
db.execute("INSERT INTO candidates VALUES('c1','u1',1,'branch','head','base','policy','requirements',1,'manifest-hash',1)")
db.execute("INSERT INTO events(transaction_id,unit_id,kind,at,payload_json) VALUES('tx1','u1','state_changed',1,'{}')")
db.execute("INSERT INTO budget_entries VALUES('b1','a1','charge',NULL,'code_rejected',1)")
db.execute("INSERT INTO budget_entries VALUES('b2','a1','correction','b1','human_verification',2)")
db.execute("INSERT INTO job_artifacts VALUES('j1','output',0,'text/plain','hash',X'6162',2)")
db.commit()

checks = {
    'duplicate active membership': "INSERT INTO unit_issues VALUES('m4','u2','1:1',1,'main','r1:1','pending',1,NULL,NULL)",
    'cross-repository membership': "INSERT INTO unit_issues VALUES('m5','u1','2:1',1,'main','r2:1','pending',1,NULL,NULL)",
    'duplicate gate same subject': "INSERT INTO unit_gates(id,unit_id,member_id,subject_key,code,created_at) VALUES('g3','u1','m1','member:m1','recovery_pending',1)",
    'gate references member of another unit': "INSERT INTO unit_gates(id,unit_id,member_id,subject_key,code,created_at) VALUES('g4','u1','m3','member:m3','recovery_pending',1)",
    'gate subject disagrees with member': "INSERT INTO unit_gates(id,unit_id,member_id,subject_key,code,created_at) VALUES('g5','u1','m1','member:m2','recovery_pending',1)",
    'mutate requirements revision': "UPDATE issue_revisions SET body='Changed' WHERE id='r1:1'",
    'membership revision belongs to another issue': "UPDATE unit_issues SET requirements_revision_id='r1:2' WHERE id='m1'",
    'current revision belongs to another issue': "UPDATE issues SET current_revision_id='r1:2' WHERE id='1:1'",
    'release before verified cleanup': "DELETE FROM job_leases WHERE slot='build-1'",
    'reassign lease': "UPDATE job_leases SET execution_fence='fence2'",
    'mutate event': "UPDATE events SET kind='other'",
    'delete event': "DELETE FROM events",
    'duplicate charge': "INSERT INTO budget_entries VALUES('b3','a1','charge',NULL,'code_rejected',3)",
    'duplicate correction': "INSERT INTO budget_entries VALUES('b4','a1','correction','b1','human_verification',3)",
    'invalid correction reference': "INSERT INTO budget_entries VALUES('b5','a1','correction','b2','human_verification',3)",
    'mutate budget': "UPDATE budget_entries SET reason_code='other'",
    'mutate candidate': "UPDATE candidates SET head_oid='another'",
    'candidate belongs to another unit': "UPDATE units SET current_candidate_id='c1' WHERE id='u2'",
    'mutate artifact': "UPDATE job_artifacts SET content=X'6163'",
    'incorrect artifact size': "INSERT INTO job_artifacts VALUES('j1','output',1,'text/plain','hash',X'6162',1)",
}
for label, sql in checks.items():
    try:
        db.execute(sql)
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
    else:
        raise AssertionError(label)
db.execute("UPDATE jobs SET fenced=1,cleanup=1,phase='cleaned' WHERE id='j1'")
db.execute("DELETE FROM job_leases WHERE slot='build-1'")
db.commit()
try:
    db.execute("INSERT INTO job_leases VALUES('build-2','j1','fence2',2)")
except sqlite3.IntegrityError:
    db.rollback()
else:
    raise AssertionError('lease for cleaned job')
assert db.execute('PRAGMA foreign_key_check').fetchall() == []
assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
print(f'Schema valid on SQLite {sqlite3.sqlite_version}; {len(checks) + 1} rejection checks passed; verified cleanup releases lease.')
