"""Small projections of operational rows; execution artifacts are explicit reads."""
import json


def deployment_views(store):
    with store.read_snapshot() as db:
        rows = db.execute('SELECT d.*,r.full_name FROM deployment_profiles d JOIN repositories r ON r.id=d.repository_id').fetchall()
        result = {}
        for row in rows:
            value = dict(name=row['full_name'], status=row['status'], workflows_digest=row['workflows_digest'], head=row['head_oid'],
                         summary=row['summary'], reason=row['reason'], error=row['error'], observed_at=row['observed_at'],
                         checked_at=row['checked_at'], reread_requested=bool(row['reread_requested']), exposure_issue=row['exposure_issue'])
            for field in ('proposal', 'observation', 'observed'):
                value[field] = json.loads(row[field + '_json']) if row[field + '_json'] else None
            result[str(row['repository_id'])] = value
        return result
