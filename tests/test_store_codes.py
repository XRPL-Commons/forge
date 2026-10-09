"""The Python vocabulary and SQL guards describe the same closed lifecycle."""
import sqlite3
import unittest
from pathlib import Path

from forge.store import codes, status
from forge.store.models import GateCode, Phase, Role


class StoreCodesTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        for schema in sorted((Path(__file__).resolve().parents[1] / 'forge/store/schema').glob('[0-9]*.sql')):
            self.db.executescript(schema.read_text())
        self.db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'example/repo')")

    def unit(self, name='u', state='building'):
        self.db.execute('INSERT INTO units(id,repository_id,base_branch,state,close_code,merge_sha,created_at,updated_at) VALUES(?,1,\'main\',?,?,?,?,?)',
                        (name, state, 'operator_closed' if state == 'closed' else None,
                         'merge' if state == 'merged' else None, 1, 1))

    def issue_and_member(self):
        self.db.execute("INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES('i',1,1,'open','r',1)")
        self.db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES('r','i','Title','','content','requirements','{}',1)")
        self.db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) VALUES('m','u','i',1,'main','r','pending',1)")

    def candidate(self):
        self.db.execute("INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES('c','u',1,'work','head','base','policy','req',1,'manifest',1)")

    def test_registry_tables_exactly_match_python_and_compatibility_enums(self):
        self.assertEqual(dict(self.db.execute('SELECT code,terminal FROM unit_states')),
                         {code: int(code in codes.TERMINAL_STATES) for code in codes.UNIT_STATES})
        self.assertEqual(set(self.db.execute('SELECT from_state,to_state FROM unit_transitions')),
                         {(old,new) for old, values in codes.TRANSITIONS.items() for new in values})
        self.assertEqual(dict(self.db.execute('SELECT code,issue_outcome FROM close_codes')),
                         {code: fact.issue_outcome for code,fact in codes.CLOSE_CODES.items()})
        self.assertEqual(set(self.db.execute('SELECT code,kind,priority,release,unit_scope,member_scope,candidate_scope FROM gate_codes')),
                         {(code,fact.kind,fact.priority,fact.release,int('unit' in fact.scopes),int('member' in fact.scopes),int('candidate' in fact.scopes)) for code,fact in codes.GATES.items()})
        for table, values in [('job_roles',codes.JOB_ROLES), ('job_phases',codes.JOB_PHASES),
                              ('part_states',codes.PART_STATES), ('exit_reasons',codes.EXIT_REASONS),
                              ('event_kinds',codes.EVENT_KINDS)]:
            self.assertEqual({row[0] for row in self.db.execute(f'SELECT code FROM {table}')}, set(values))
        self.assertEqual(dict(self.db.execute('SELECT code,retention FROM artifact_kinds')), codes.ARTIFACT_KINDS)
        for enum, values in [(Phase,codes.UNIT_STATES),(Role,codes.JOB_ROLES),(GateCode,codes.GATES)]:
            self.assertEqual({item.value for item in enum}, set(values))
        self.assertEqual(set(status.WORDING), status.registered_codes())
        self.assertEqual(codes.RAW_LOG_RETENTION_DAYS, 30)
        self.assertEqual(codes.ARTIFACT_KINDS['result'], 'durable')
        self.assertEqual(codes.ARTIFACT_KINDS['launch_input'], 'durable')

    def test_every_allowed_and_disallowed_phase_edge_is_enforced_by_sql(self):
        for before in codes.UNIT_STATES:
            for after in codes.UNIT_STATES:
                with self.subTest(before=before, after=after):
                    name = f'{before}:{after}'
                    self.unit(name, before)
                    args = (after, 'operator_closed' if after == 'closed' else None,
                            'merge' if after == 'merged' else None, name)
                    statement = 'UPDATE units SET state=?,close_code=?,merge_sha=? WHERE id=?'
                    if before == after or after in codes.TRANSITIONS[before]:
                        self.db.execute(statement, args)
                        self.assertEqual(self.db.execute('SELECT state FROM units WHERE id=?',(name,)).fetchone()[0], after)
                    else:
                        with self.assertRaises(sqlite3.IntegrityError):
                            self.db.execute(statement, args)

    def test_every_gate_scope_matches_registry(self):
        self.unit(); self.issue_and_member(); self.candidate()
        for code, fact in codes.GATES.items():
            for scope in ('unit','member','candidate'):
                with self.subTest(code=code, scope=scope):
                    args=(f'{code}:{scope}', 'm' if scope=='member' else None,
                          'c' if scope=='candidate' else None,
                          {'unit':'unit','member':'member:m','candidate':'candidate:c'}[scope], code)
                    statement="INSERT INTO unit_gates(id,unit_id,member_id,candidate_id,subject_key,code,created_at) VALUES(?,'u',?,?,?,?,1)"
                    if scope in fact.scopes:
                        self.db.execute(statement,args)
                    else:
                        with self.assertRaises(sqlite3.IntegrityError):
                            self.db.execute(statement,args)

    def test_registry_cannot_be_extended_or_rewritten_by_runtime_sql(self):
        for statement in ("INSERT INTO unit_states VALUES('invented',0)",
                          "UPDATE gate_codes SET release='observation' WHERE code='operator_pause'",
                          "DELETE FROM unit_transitions WHERE from_state='queued'",
                          "INSERT INTO event_kinds VALUES('invented')"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement)

    def test_gate_release_cannot_bypass_an_operator_pause_or_rewrite_history(self):
        self.unit()
        self.db.execute("INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) VALUES('g','u','unit','operator_pause',1)")
        for statement in ("UPDATE unit_gates SET resolved_at=2,resolution='observation'",
                          "UPDATE unit_gates SET code='environment'",
                          "UPDATE unit_gates SET resolved_at=2"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement)
        self.db.execute("UPDATE unit_gates SET resolved_at=2,resolution='operator'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE unit_gates SET resolved_at=3,resolution='operator'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM unit_gates')

    def test_terminal_state_requires_release_and_preserves_close_provenance(self):
        self.unit(); self.issue_and_member()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE units SET state='closed',close_code='github_completed'")
        self.db.execute("UPDATE unit_issues SET left_at=2,exit_reason='closed',state='done'")
        self.db.execute("INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) VALUES('g','u','unit','operator_pause',1)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE units SET state='closed',close_code='github_completed'")
        self.db.execute("UPDATE unit_gates SET resolved_at=2,resolution='superseded'")
        self.db.execute("UPDATE units SET state='closed',close_code='github_completed'")
        for statement in ("UPDATE units SET close_code='operator_closed'",
                          "UPDATE unit_issues SET left_at=NULL,exit_reason=NULL",
                          'DELETE FROM unit_issues',
                          "INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) VALUES('again','u','unit','operator_pause',3)"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement)

    def test_terminal_facts_are_required_and_not_permitted_on_open_units(self):
        self.unit()
        for statement in ("UPDATE units SET state='closed'", "UPDATE units SET merge_sha='merge'",
                          "UPDATE units SET close_code='operator_closed'",
                          "UPDATE units SET state='merged',merge_sha=''",
                          "UPDATE units SET state='closed',close_code='unknown'"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement)

    def test_older_issue_observations_cannot_overwrite_newer_facts(self):
        self.unit(); self.issue_and_member()
        self.db.execute("UPDATE issues SET remote_updated_at='2026-10-06T10:00:00Z',collection_generation=2")
        for timestamp,generation in [('2026-10-06T09:59:59Z',3),('2026-10-06T10:00:00Z',1),(None,4)]:
            with self.subTest(timestamp=timestamp,generation=generation), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('UPDATE issues SET github_state=\'closed\',remote_updated_at=?,collection_generation=?',(timestamp,generation))
        self.db.execute("UPDATE issues SET github_state='closed',remote_updated_at='2026-10-06T10:00:00Z',collection_generation=3")
        self.assertEqual(self.db.execute('SELECT github_state FROM issues').fetchone()[0], 'closed')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE issues SET github_number=2')

    def test_job_cannot_restart_or_change_outcome_and_terminal_transition_fences_it(self):
        self.unit(); self.issue_and_member()
        self.db.execute("INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) VALUES('a','u','m',1,1,'{}',1,1)")
        self.db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,created_at) VALUES('j','u','a','m','build','running',1)")
        self.db.execute("UPDATE unit_issues SET left_at=2,exit_reason='closed'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE units SET state='closed',close_code='operator_closed'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE jobs SET phase='reserved'")
        self.db.execute("UPDATE jobs SET fenced=1")
        self.db.execute("UPDATE units SET state='closed',close_code='operator_closed'")
        self.db.execute("UPDATE jobs SET phase='finished',outcome='stale_failure'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE jobs SET outcome='success'")
        self.db.execute("UPDATE jobs SET phase='cleaned',cleanup=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE jobs SET phase='finished'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM jobs')

    def test_wording_has_plain_fallback_and_rejects_unknown_codes(self):
        for code in status.registered_codes():
            self.assertIsInstance(status.describe(code), str)
            self.assertNotIn('{', status.describe(code))
        self.assertEqual(status.describe('reviewing',pull=42), 'Reviewing pull request #42')
        self.assertEqual(status.describe('reviewing',wrong=42), 'In review')
        with self.assertRaises(KeyError):
            status.describe('unknown')


if __name__ == '__main__':
    unittest.main()
