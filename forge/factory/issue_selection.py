"""Canonical status selection before counts and pagination, in the caller's snapshot.

SQLite narrows ordinary rows cheaply. Approved deliveries and other assignments
with dependencies use exact lifecycle/output authority; only their selected
issues need Python facts.
"""
import json

from ..store.queries import Queries, IssueGateFacts, MAX_PAGE_SIZE
from ..store.job_scope import issue_jobs_in
from .status_projection import public_status


# Start from sparse edges and approved-delivery identities, not the inventory.
# All grouped members need canonical shared gates and exact child job scope,
# including siblings with no dependency edges of their own.
CANONICAL_UNITS = """SELECT d.unit_id FROM member_dependencies d
 JOIN units u ON u.id=d.unit_id
 WHERE d.membership_generation=u.membership_generation AND u.state NOT IN ('merged','closed','dissolved')
 UNION SELECT m.unit_id FROM issue_dependencies d
 JOIN unit_issues m ON m.issue_id=d.issue_id AND m.requirements_revision_id=d.requirements_revision_id AND m.left_at IS NULL
 JOIN units u ON u.id=m.unit_id WHERE u.state NOT IN ('merged','closed','dissolved')
 UNION SELECT u.id FROM unit_publication_targets p JOIN units u ON u.id=p.unit_id
 WHERE u.state NOT IN ('merged','closed','dissolved')"""


class IssueSelection:
    def __init__(self, db, *, repository_id=None, issue_id=None, query='', enabled_only=False,
                 github_state=None, terminal_only=False):
        self.db = db
        self._contexts = {}
        self._gate_facts = IssueGateFacts(db)
        overrides = []
        if not terminal_only:
            clauses = ['m.left_at IS NULL']
            parameters = []
            for expression, value in [('i.repository_id=?', repository_id), ('i.id=?', issue_id),
                                      ('i.github_state=?', github_state)]:
                if value is not None:
                    clauses.append(expression); parameters.append(value)
            if enabled_only:
                clauses.append('r.enabled=1')
            if query:
                clauses.append("instr(lower(p.title||' '||r.full_name||' #'||i.github_number),lower(?))>0")
                parameters.append(query)
            candidates = db.execute('WITH canonical_units AS MATERIALIZED (' + CANONICAL_UNITS + ') '
                'SELECT i.id FROM canonical_units d CROSS JOIN unit_issues m ON m.unit_id=d.unit_id '
                'JOIN issues i ON i.id=m.issue_id JOIN repositories r ON r.id=i.repository_id '
                'JOIN issue_revisions p ON p.id=i.current_revision_id WHERE ' + ' AND '.join(clauses) + ' ORDER BY i.id', parameters)
            while identifiers := [row[0] for row in candidates.fetchmany(MAX_PAGE_SIZE)]:
                contexts = Queries._issue_contexts(db, identifiers, gate_facts=self._gate_facts)
                self._contexts.update(contexts)
                for identifier, (issue, unit) in contexts.items():
                    jobs = issue_jobs_in(db, unit['id'], identifier)
                    active = any(not row['cleanup'] and not row['fenced'] and row['outcome'] is None for row in jobs)
                    overrides.append([identifier, issue['lifecycle']['reason_code'], public_status(issue, active)])
        self.parameters = (json.dumps(overrides, separators=(',', ':')),) if overrides else ()
        if overrides:
            self.ctes = """canonical_status AS MATERIALIZED (
 SELECT value->>0 AS id,value->>1 AS reason_code,value->>2 AS public_status FROM json_each(?)),
 public_issue_index AS NOT MATERIALIZED (
 SELECT x.*,CASE WHEN s.id IS NOT NULL THEN s.reason_code ELSE x.indexed_reason_code END AS reason_code,
 CASE WHEN s.id IS NOT NULL THEN s.public_status ELSE x.indexed_status END AS public_status
 FROM issue_selection_facts x LEFT JOIN canonical_status s ON s.id=x.id)"""
        else:
            self.ctes = ('public_issue_index AS NOT MATERIALIZED (SELECT x.*,x.indexed_reason_code AS reason_code,'
                         'x.indexed_status AS public_status FROM issue_selection_facts x)')

    def execute(self, sql, parameters=(), *, explain=False):
        # Existing bounded terminal-member selection remains part of this query.
        statement = 'WITH ' + self.ctes + (',' + sql[5:] if sql.startswith('WITH ') else ' ' + sql)
        return self.db.execute(('EXPLAIN QUERY PLAN ' if explain else '') + statement, (*self.parameters, *parameters))

    def contexts(self, identifiers):
        missing = [identifier for identifier in identifiers if identifier not in self._contexts]
        self._contexts.update(Queries._issue_contexts(self.db, missing, gate_facts=self._gate_facts))
        return {identifier: self._contexts[identifier] for identifier in identifiers}
