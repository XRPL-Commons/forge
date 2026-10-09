"""Bounded supporting-domain reads shared by adapters and migration parity."""
import json

from .queries import _limit


def message(row):
    return {**json.loads(row['metadata_json']), 'id':row['id'], 'sequence':row['sequence'],
            'role':'user' if row['role']=='operator' else row['role'],
            'text':row['content'], 'at':row['created_at']}


class SupportingQueries:
    def __init__(self, store):
        self.store = store

    def messages(self, issue_id, *, before=None, limit=20):
        limit = _limit(limit)
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError('before must be a positive message sequence')
        with self.store.read_snapshot() as db:
            thread = db.execute('SELECT id FROM discussions WHERE issue_id=?', (issue_id,)).fetchone()
            if not thread:
                return {'messages':[], 'before':None}
            rows = db.execute('SELECT * FROM messages WHERE discussion_id=? ' +
                              ('AND sequence < ? ' if before is not None else '') +
                              'ORDER BY sequence DESC LIMIT ?',
                              (thread[0], *((before,) if before is not None else ()), limit+1)).fetchall()
            page = list(reversed(rows[:limit]))
            return {'messages':[message(row) for row in page],
                    'before':page[0]['sequence'] if len(rows)>limit else None}

    def inventory_scan(self, observation_id, *, after=None, limit=50):
        """Read one page of captured scan facts without import archives."""
        limit = _limit(limit)
        if after is not None and not isinstance(after, str):
            raise ValueError('after must be an issue key')
        with self.store.read_snapshot() as db:
            observation = db.execute("SELECT details_json,cursor FROM repository_observations WHERE id=? AND kind='inventory'", (observation_id,)).fetchone()
            if observation is None:
                return None
            details = json.loads(observation['details_json'])
            rows = db.execute('SELECT * FROM inventory_scan_issues WHERE observation_id=? ' +
                              ('AND issue_key>? ' if after is not None else '') +
                              'ORDER BY issue_key LIMIT ?',
                              (observation_id, *((after,) if after is not None else ()), limit+1)).fetchall()
            items = []
            for row in rows[:limit]:
                facts = json.loads(row['fields_json'])
                if row['body_present']:
                    facts['body'] = row['body']
                items.append({'key':row['issue_key'], 'facts':facts})
            return {'present':'scan' in details, 'header':details.get('scan'),
                    'issues_present':details.get('scan_issues_present',False),
                    'cursor':json.loads(observation['cursor']) if observation['cursor'] else None,
                    'items':items, 'next_cursor':items[-1]['key'] if len(rows)>limit else None}

    def action(self, request_id):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT a.*,i.github_number,i.repository_id,r.full_name FROM issue_action_requests a '
                             'JOIN issues i ON i.id=a.issue_id JOIN repositories r ON r.id=i.repository_id WHERE a.id=?',
                             (request_id,)).fetchone()
            if row is None:
                return None
            value = dict(action=row['action'],created_at=row['created_at'],key=row['issue_id'],
                         number=row['github_number'],repo_id=row['repository_id'],repo=row['full_name'],
                         revision=row['expected_revision'],status=row['status'],reason=row['reason'])
            if row['message'] is not None:
                value['message'] = row['message']
            for key in ('plan','environment'):
                if row[key+'_json'] is not None:
                    value[key] = json.loads(row[key+'_json'])
            return value

    def notification(self, event_id):
        with self.store.read_snapshot() as db:
            rows = db.execute('SELECT * FROM notification_outbox WHERE source_event_id=? ORDER BY channel', (event_id,)).fetchall()
            if not rows:
                return None
            result = {'id':event_id,'created_at':rows[0]['created_at'],
                      'message':json.loads(rows[0]['payload_json'])['message'],'channels':{}}
            for row in rows:
                receipt = dict(status=row['delivery_state'],attempts=row['attempt_count'],next_at=row['retry_at'])
                if row['last_attempt_at'] is not None:
                    receipt['last_attempt_at'] = row['last_attempt_at']
                result['channels'][row['channel']] = receipt
            return result
