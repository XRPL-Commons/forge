"""Relational safety boundaries independent of the store's Python API."""
import sqlite3
import unittest
from pathlib import Path


SCHEMA = Path(__file__).resolve().parents[1] / 'forge/store/schema/001_initial.sql'


class StoreSchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA.read_text())
        self.insert('repositories', id=1, full_name='example/one')
        self.insert('repositories', id=2, full_name='example/two')
        for name, repo, number in [('a', 1, 1), ('b', 1, 2), ('c', 2, 1)]:
            self.issue(name, repo, number)
        self.unit('u', 1)
        self.unit('v', 1)
        self.unit('w', 2)
        self.member('ma', 'u', 'a', 1)
        self.member('mb', 'u', 'b', 1)
        self.member('mc', 'w', 'c', 2)
        self.candidate('ca', 'u')
        self.candidate('cc', 'w')
        self.attempt('aa', 'u', 'ma')
        self.attempt('ab', 'u', 'mb')
        self.attempt('ac', 'w', 'mc')
        self.job('ja', 'u', 'aa', 'ma')
        self.job('jb', 'u', 'ab', 'mb')
        self.db.commit()

    def insert(self, table, **values):
        columns = ', '.join(values)
        placeholders = ', '.join('?' for _ in values)
        self.db.execute(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', tuple(values.values()))

    def issue(self, name, repo, number):
        self.insert('issues', id=name, repository_id=repo, github_number=number,
                    github_state='open', current_revision_id=f'r{name}', observed_at=1)
        self.insert('issue_revisions', id=f'r{name}', issue_id=name, title=name,
                    body='', content_hash=name, requirements_hash=name,
                    requirements_json='{}', observed_at=1)

    def unit(self, name, repo):
        self.insert('units', id=name, repository_id=repo, base_branch='main',
                    state='building', created_at=1, updated_at=1)

    def member(self, name, unit, issue, repo, **extra):
        values = dict(id=name, unit_id=unit, issue_id=issue, repository_id=repo,
                      base_branch='main', requirements_revision_id=f'r{issue}',
                      state='pending', joined_at=1)
        values.update(extra)
        self.insert('unit_issues', **values)

    def candidate(self, name, unit):
        self.insert('candidates', id=name, unit_id=unit, pull_number=1, branch='work',
                    head_oid='head', base_oid='base', policy_hash='policy',
                    requirements_hash='requirements', membership_generation=1,
                    input_manifest_hash='manifest', accepted_at=1)

    def attempt(self, name, unit, member):
        self.insert('attempts', id=name, unit_id=unit, member_id=member,
                    membership_generation=1, assignment_generation=1,
                    input_manifest_json='{}', budget_generation=1, created_at=1)

    def job(self, name, unit, attempt, member):
        self.insert('jobs', id=name, unit_id=unit, attempt_id=attempt, member_id=member,
                    role='build', phase='reserved', created_at=1)

    def rejected(self, operation):
        # Savepoints also force deferred ownership failures without losing fixtures.
        self.db.execute('SAVEPOINT rejection')
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                operation()
                if self.db.execute('PRAGMA foreign_key_check').fetchone():
                    raise sqlite3.IntegrityError('deferred foreign key violation')
        finally:
            self.db.execute('ROLLBACK TO rejection')
            self.db.execute('RELEASE rejection')

    def gate(self, name, unit='u', member=None, candidate=None, subject='unit'):
        self.insert('unit_gates', id=name, unit_id=unit, member_id=member,
                    candidate_id=candidate, subject_key=subject,
                    code='recovery_pending', created_at=1)

    def test_membership_is_exclusive_and_repository_base_revision_owned(self):
        self.rejected(lambda: self.member('duplicate', 'v', 'a', 1))
        self.rejected(lambda: self.member('cross_repo', 'u', 'c', 2, left_at=2, exit_reason='dropped'))
        self.rejected(lambda: self.member('cross_base', 'v', 'a', 1, base_branch='other', left_at=2, exit_reason='dropped'))
        self.rejected(lambda: self.member('cross_revision', 'v', 'a', 1, requirements_revision_id='rb', left_at=2, exit_reason='dropped'))
        self.db.execute("UPDATE unit_issues SET left_at=2, exit_reason='dropped' WHERE id='ma'")
        self.member('new', 'v', 'a', 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM unit_issues WHERE issue_id='a'").fetchone()[0], 2)

    def test_unit_pointers_cannot_select_another_units_candidate_or_gate(self):
        self.gate('gw', unit='w')
        for column, value in [('current_candidate_id', 'cc'), ('primary_gate_id', 'gw')]:
            with self.subTest(column=column):
                self.rejected(lambda: self.db.execute(f'UPDATE units SET {column}=? WHERE id=?', (value, 'u')))
        self.db.execute("UPDATE units SET current_candidate_id='ca' WHERE id='u'")
        self.db.commit()

    def test_member_scoped_gates_allow_independent_recovery_and_enforce_subject(self):
        self.gate('ga', member='ma', subject='member:ma')
        self.gate('gb', member='mb', subject='member:mb')
        self.rejected(lambda: self.gate('duplicate', member='ma', subject='member:ma'))
        self.rejected(lambda: self.gate('wrong_subject', member='ma', subject='member:mb'))
        self.rejected(lambda: self.gate('wrong_unit', member='mc', subject='member:mc'))
        self.gate('candidate', candidate='ca', subject='candidate:ca')
        self.rejected(lambda: self.gate('wrong_candidate', candidate='cc', subject='candidate:cc'))
        self.db.execute("UPDATE unit_gates SET resolved_at=2 WHERE id='ga'")
        self.gate('new', member='ma', subject='member:ma')

    def test_jobs_match_the_attempt_member_and_assignment_is_immutable(self):
        self.rejected(lambda: self.job('wrong', 'u', 'aa', 'mb'))
        self.rejected(lambda: self.job('wrong_unit', 'w', 'aa', 'mc'))
        self.rejected(lambda: self.db.execute("UPDATE jobs SET member_id='mb' WHERE id='ja'"))
        self.rejected(lambda: self.db.execute("UPDATE attempts SET assignment_generation=2 WHERE id='aa'"))
        self.rejected(lambda: self.db.execute("DELETE FROM attempts WHERE id='ac'"))

    def test_capacity_is_exclusive_and_fencing_does_not_release_before_cleanup(self):
        self.insert('job_leases', slot='build:1', job_id='ja', execution_fence='f1', acquired_at=1)
        self.rejected(lambda: self.insert('job_leases', slot='build:1', job_id='jb', execution_fence='f2', acquired_at=1))
        self.rejected(lambda: self.db.execute("UPDATE job_leases SET job_id='jb'"))
        self.rejected(lambda: self.db.execute('DELETE FROM job_leases'))
        self.db.execute("UPDATE jobs SET fenced=1 WHERE id='ja'")
        self.rejected(lambda: self.db.execute('DELETE FROM job_leases'))
        self.db.execute("UPDATE jobs SET phase='cleaned', cleanup=1 WHERE id='ja'")
        self.db.execute('DELETE FROM job_leases')
        self.rejected(lambda: self.insert('job_leases', slot='build:1', job_id='ja', execution_fence='again', acquired_at=2))
        self.rejected(lambda: self.db.execute("UPDATE jobs SET fenced=0,cleanup=0 WHERE id='ja'"))
        self.insert('job_leases', slot='build:1', job_id='jb', execution_fence='f2', acquired_at=2)

    def test_discussion_and_pipeline_share_capacity_without_sharing_failure_budget(self):
        self.insert('discussions', id='d', title='Plan', created_at=1, updated_at=1)
        self.insert('discussion_executions', id='dx', discussion_id='d', phase='running', created_at=1)
        self.insert('discussion_leases', slot='provider:1', execution_id='dx', execution_fence='discussion', acquired_at=1)
        self.rejected(lambda: self.insert('job_leases', slot='provider:1', job_id='ja', execution_fence='pipeline', acquired_at=1))
        self.rejected(lambda: self.db.execute('DELETE FROM discussion_leases'))
        self.db.execute("UPDATE discussion_executions SET fenced=1,cleanup=1,phase='cleaned'")
        self.db.execute('DELETE FROM discussion_leases')
        self.insert('job_leases', slot='provider:1', job_id='ja', execution_fence='pipeline', acquired_at=2)
        self.insert('discussion_executions', id='dy', discussion_id='d', phase='running', created_at=2)
        self.rejected(lambda: self.insert('discussion_leases', slot='provider:1', execution_id='dy', execution_fence='discussion2', acquired_at=2))

    def test_budget_charges_and_corrections_are_unique_owned_and_append_only(self):
        self.insert('budget_entries', id='charge', attempt_id='aa', kind='charge', reason_code='failure', created_at=1)
        self.rejected(lambda: self.insert('budget_entries', id='again', attempt_id='aa', kind='charge', reason_code='failure', created_at=1))
        self.rejected(lambda: self.insert('budget_entries', id='wrong', attempt_id='ab', kind='correction', corrects_id='charge', reason_code='environment', created_at=2))
        self.insert('budget_entries', id='correction', attempt_id='aa', kind='correction', corrects_id='charge', reason_code='environment', created_at=2)
        self.rejected(lambda: self.insert('budget_entries', id='twice', attempt_id='aa', kind='correction', corrects_id='charge', reason_code='environment', created_at=2))
        self.rejected(lambda: self.db.execute('DELETE FROM budget_entries'))
        self.rejected(lambda: self.db.execute("UPDATE budget_entries SET reason_code='other'"))

    def test_generations_are_positive_and_never_go_backwards(self):
        for column in ('membership_generation', 'fencing_generation', 'budget_generation', 'version'):
            with self.subTest(column=column):
                self.rejected(lambda: self.db.execute(f'UPDATE units SET {column}=0'))
                self.db.execute(f'UPDATE units SET {column}=2')
                self.rejected(lambda: self.db.execute(f'UPDATE units SET {column}=1'))

    def test_candidate_evidence_and_part_output_cannot_cross_unit_or_member(self):
        evidence = dict(id='e', unit_id='u', candidate_id='cc', kind='audit', verdict='pass', actor_id='worker', observed_at=1)
        self.rejected(lambda: self.insert('candidate_evidence', **evidence))
        evidence['candidate_id'] = 'ca'
        self.insert('candidate_evidence', **evidence)
        self.rejected(lambda: self.db.execute("UPDATE candidate_evidence SET verdict='fail'"))
        self.rejected(lambda: self.db.execute('DELETE FROM candidate_evidence'))
        output = dict(id='o', unit_id='u', member_id='ma', attempt_id='ab', requirements_revision_id='ra', membership_generation=1, commit_oid='commit', base_oid='base', input_manifest_hash='manifest', accepted_at=1)
        self.rejected(lambda: self.insert('part_outputs', **output))
        output['attempt_id'] = 'aa'
        self.insert('part_outputs', **output)
        self.rejected(lambda: self.db.execute("UPDATE part_outputs SET commit_oid='other'"))

    def test_required_history_cannot_be_deleted_or_rewritten(self):
        self.insert('events', transaction_id='t', unit_id='u', kind='created', at=1, payload_json='{}')
        for table, change in [('events', "kind='changed'"), ('candidates', "head_oid='other'"), ('issue_revisions', "title='other'")]:
            with self.subTest(table=table):
                self.rejected(lambda: self.db.execute(f'UPDATE {table} SET {change}'))
                self.rejected(lambda: self.db.execute(f'DELETE FROM {table}'))

    def test_artifacts_are_bounded_chunks_with_exact_size_and_retention(self):
        artifact = dict(job_id='ja', kind='result', sequence=0, media_type='application/json', digest='digest', content=b'{}', size=2)
        self.insert('job_artifacts', **artifact)
        self.rejected(lambda: self.db.execute("UPDATE job_artifacts SET content=x'00',size=1"))
        self.rejected(lambda: self.db.execute('DELETE FROM job_artifacts'))
        artifact.update(sequence=1, size=3)
        self.rejected(lambda: self.insert('job_artifacts', **artifact))
        artifact.update(content=b'x' * 1048577, size=1048577)
        self.rejected(lambda: self.insert('job_artifacts', **artifact))
        artifact.update(content=b'x' * 1048576, size=1048576)
        self.insert('job_artifacts', **artifact)

    def test_json_fields_reject_invalid_or_oversize_utf8_payloads(self):
        self.gate('g')
        for value in ('invalid', '[]', '{"note":"' + '\u00e9' * 33000 + '"}'):
            with self.subTest(size=len(value)):
                self.rejected(lambda: self.db.execute('UPDATE unit_gates SET details_json=?', (value,)))
        self.rejected(lambda: self.db.execute("UPDATE issues SET labels_json='{}'"))
        self.rejected(lambda: self.insert('events', transaction_id='t', kind='bad', at=1, payload_json='bad'))

    def test_all_domain_tables_are_strict_and_schema_is_foreign_key_clean(self):
        domains = {'configuration', 'operator_sessions', 'client_connections', 'operator_audit',
                   'runtime_controls', 'environment_proposals', 'repository_observations',
                   'import_runs', 'import_sources', 'import_progress', 'notification_outbox',
                   'notification_deliveries', 'external_effects', 'check_runs', 'merge_requests'}
        tables = {row[1]: row[5] for row in self.db.execute('PRAGMA table_list') if row[1] != 'sqlite_schema'}
        self.assertTrue(domains <= tables.keys())
        self.assertTrue(all(strict == 1 for name, strict in tables.items() if not name.startswith('sqlite_')))
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
