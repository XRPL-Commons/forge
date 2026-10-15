"""Retained subject decisions; existing proposal/thread receipts apply choices."""
import json

from ..steward_contract import digest, encoded, require


def save_in(db, *, kind, subject_id, version, execution_id, dossier, decision, now):
    require(dossier['subject']['kind']==kind,'Decision subject changed')
    payload=(kind,subject_id,version,execution_id,encoded(dossier).decode(),encoded(decision).decode(),digest(decision),now)
    db.execute('INSERT INTO steward_subject_decisions VALUES(?,?,?,?,?,?,?,?)',payload)


def read_in(db, kind, subject_id):
    row=db.execute('SELECT * FROM steward_subject_decisions WHERE subject_kind=? AND subject_id=? '
        'ORDER BY subject_version DESC,created_at DESC,execution_id DESC LIMIT 1',(kind,subject_id)).fetchone()
    if row is None:return None
    output=json.loads(row['decision_json'])
    require(digest(output)==row['digest'],'Retained subject decision changed')
    return dict(decision=output,dossier=json.loads(row['dossier_json']),digest=row['digest'],execution_id=row['execution_id'])
