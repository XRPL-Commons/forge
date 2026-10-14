"""Bounded card evidence collected once in the projection's existing snapshot.

These are presentation inputs, not a second lifecycle/status evaluator. Exact
unit/revision, candidate and immutable job scope still belong to their readers.
"""
import json


# The -> operator returns JSON text: absent keys are SQL NULL, while explicit
# JSON null is the text "null". Keep this distinction and JSON value types.
EVENT_METADATA_SQL = """e.kind,e.at,
 e.payload_json -> '$.provider' AS provider_json,
 e.payload_json -> '$.model' AS model_json"""


def event_metadata(row):
    result = {'at': row['at'], 'kind': row['kind']}
    for field in ('provider', 'model'):
        value = row[field + '_json']
        if value is not None:
            result[field] = json.loads(value)
    return result


class ActivityReads:
    def __init__(self, db, contexts):
        pairs = [[issue['id'], unit['id'] if unit else None, issue['current_revision_id']]
                 for issue, unit in contexts.values()]
        requested = json.dumps(pairs)
        units = {unit['id']: unit for _, unit in contexts.values() if unit}
        unit_ids = json.dumps(list(units))
        candidate_ids = json.dumps(list({unit['current_candidate_id'] for unit in units.values()
                                         if unit['current_candidate_id']}))
        # Probe each retained member's newest exact-revision observation through
        # dependency_current. This includes former memberships, without scanning
        # all retained checks for each card or materializing their full history.
        self.checks = {row['issue_id']: row for row in db.execute("""
            WITH requested AS MATERIALIZED (
              SELECT value->>0 AS issue_id,value->>1 AS unit_id,value->>2 AS revision_id FROM json_each(?)
            ), latest AS (
              SELECT p.issue_id,d.facts_json,row_number() OVER (
                PARTITION BY p.issue_id ORDER BY d.generation DESC,d.rowid) AS position
              FROM requested p JOIN unit_issues m ON m.unit_id=p.unit_id
              JOIN dependency_checks d ON d.id=(
                SELECT c.id FROM dependency_checks c
                WHERE c.member_id=m.id AND c.requirements_revision_id=p.revision_id AND c.unit_id=p.unit_id
                ORDER BY c.generation DESC LIMIT 1)
            ) SELECT issue_id,facts_json FROM latest WHERE position=1
            """, (requested,))}
        self.orders = {row['issue_id']: json.loads(row['references_json']) for row in db.execute("""
            SELECT p.value->>0 AS issue_id,o.references_json FROM json_each(?) p
            JOIN prerequisite_orders o ON o.id=(SELECT id FROM prerequisite_orders
              WHERE issue_id=p.value->>0 AND requirements_revision_id=p.value->>2
              ORDER BY generation DESC LIMIT 1)
            """, (requested,))}
        self.imported = {}
        missing = {unit['id'] for issue, unit in contexts.values() if unit and issue['id'] not in self.checks}
        if missing:
            for row in db.execute('SELECT unit_id,kind,value_json FROM dependency_observation_members '
                                  'WHERE unit_id IN (SELECT value FROM json_each(?))', (json.dumps(list(missing)),)):
                self.imported.setdefault(row['unit_id'], []).append(row)
        self.unit_contexts = {row['unit_id']: row for row in db.execute(
            'SELECT unit_id,summary_json,last_observed_at FROM unit_context WHERE unit_id IN (SELECT value FROM json_each(?))', (unit_ids,))}
        self.charges = {row['unit_id']: row['charges'] for row in db.execute(
            "SELECT a.unit_id,SUM(CASE b.kind WHEN 'charge' THEN 1 ELSE -1 END) AS charges "
            'FROM units u JOIN attempts a ON a.unit_id=u.id AND a.budget_generation=u.budget_generation '
            'JOIN budget_entries b ON b.attempt_id=a.id WHERE u.id IN (SELECT value FROM json_each(?)) GROUP BY a.unit_id', (unit_ids,))}
        self.candidates = {row['id']: row for row in db.execute(
            'SELECT id,pull_number,head_oid FROM candidates WHERE id IN (SELECT value FROM json_each(?))', (candidate_ids,))}
        self.deployments = {row['candidate_id']: row for row in db.execute(
            "SELECT d.* FROM json_each(?) p JOIN deployment_observations d ON d.rowid=("
            "SELECT rowid FROM deployment_observations WHERE candidate_id=p.value ORDER BY (status='success') DESC,observed_at DESC LIMIT 1)", (candidate_ids,))}
        self.events = {}
        for row in db.execute('SELECT e.unit_id,' + EVENT_METADATA_SQL + ' FROM json_each(?) p JOIN events e '
                'ON e.sequence IN (SELECT sequence FROM events WHERE unit_id=p.value ORDER BY sequence DESC LIMIT 3) '
                'ORDER BY e.unit_id,e.sequence DESC', (unit_ids,)):
            self.events.setdefault(row['unit_id'], []).append(event_metadata(row))
