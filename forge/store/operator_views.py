"""Exact candidate operator receipts for public views and ownership admission."""
import json


def reviewer_request_in(db,candidate_id):
    if not candidate_id:return None
    row=db.execute('SELECT * FROM candidate_reviewer_requests WHERE candidate_id=? ORDER BY requested_at DESC,request_id DESC LIMIT 1',(candidate_id,)).fetchone()
    if row is None:return None
    return dict(candidate_id=candidate_id,logins=json.loads(row['logins_json']),at=row['requested_at'],
                request=row['request_id'],issue_assignment=bool(row['issue_assignment']))


def ownership_context(store,item,candidate_id):
    with store.read_snapshot() as db:receipt=reviewer_request_in(db,candidate_id)
    return {**item,'reviewers_requested':receipt} if receipt else item
