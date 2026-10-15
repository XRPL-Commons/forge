"""Operator effects retain uncertain sends and receipt history in the shared outbox."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.github import GitHubUnavailable
from forge.store import db as storage, upgrade
from tests import test_store_operator as fixtures


class OperatorOutboxTests(unittest.TestCase):
    setUp=fixtures.OperatorTests.setUp
    command=fixtures.OperatorTests.command
    view=fixtures.OperatorTests.view
    reserve=fixtures.OperatorTests.reserve
    revision=fixtures.OperatorTests.revision
    request=fixtures.OperatorTests.request
    status=fixtures.OperatorTests.status

    def lose_comment(self):
        request=self.request('close')
        self.github.after_write=lambda: (_ for _ in ()).throw(GitHubUnavailable('lost reply'))
        self.assertEqual(self.actions.step(request)['pending'],'github_observation')
        self.github.after_write=None
        return request

    def test_unknown_comment_send_is_not_repeated_or_confirmed_by_another_actor(self):
        request=self.lose_comment()
        original=self.github.comments.pop()
        self.github.calls.clear()
        self.assertEqual(self.actions.step(request)['pending'],'github_observation')
        self.assertEqual(self.github.calls,[])
        self.github.comments.append({**original,'user':{'login':'human','type':'User'}})
        self.assertEqual(self.actions.step(request)['pending'],'github_observation')
        self.assertEqual(self.github.calls,[])
        self.github.comments[:]=[original]
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertFalse(any(method=='POST' and path.endswith('/comments') for method,path,_ in self.github.calls))

    def test_rejected_request_keeps_readback_ownership_without_authorizing_new_writes(self):
        request=self.lose_comment()
        original=self.github.comments.pop()
        self.actions._reject(self.actions._request(request),'Authority changed')
        self.github.calls.clear()
        result=self.actions.cycle()['items'][0]
        self.assertEqual(result['pending'],'effect_reconciliation')
        self.assertEqual(self.github.calls,[])
        self.assertEqual(self.actions.cycle()['items'],[])  # Durable readback backoff.
        self.github.comments.append(original);self.now+=31
        self.actions.cycle()
        with self.store.read_snapshot() as db:
            effect=db.execute('SELECT status,attempts FROM external_effects WHERE operator_request_id=?',(request,)).fetchone()
            self.assertEqual(tuple(effect),('confirmed',1))
        self.assertEqual(self.status(request)['status'],'rejected')
        self.assertEqual(self.github.calls,[])


class OperatorOutboxMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_payloads_receipts_and_marks_legacy_pending_as_uncertain(self):
        catalog=storage._migrations()
        with tempfile.TemporaryDirectory() as directory:
            with patch('forge.store.db._migrations',return_value=catalog[:3]):
                store=storage.Store.create(Path(directory)/'state')
            with store.write() as db:
                db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'owner/repo')")
                db.execute("INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES('i',1,1,'open','r',1)")
                db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES('r','i','Title','','content','requirements','{}',1)")
                db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES('u',1,'main','building',1,1)")
                db.execute("INSERT INTO issue_action_requests(id,issue_id,action,expected_revision,status,reason,plan_json,created_at) "
                           "VALUES('q','i','close','revision','pending','reason','{}',1)")
                db.execute("INSERT INTO operator_action_authority(request_id,actor_id,unit_id,expected_issue_version,payload_digest,updated_at) "
                           "VALUES('q','github:42','u',1,'digest',1)")
                for index,state in enumerate(('pending','confirmed','uncertain')):
                    db.execute('INSERT INTO operator_action_effects VALUES(?,?,?,?,?,?,?)',
                        ('q',str(index),state,json.dumps({'original':index}),json.dumps({'closure_event':{'id':42}}) if state=='confirmed' else None,2,3))
            upgraded=storage.Store(store.root)
            upgrade.apply(upgraded);upgrade.apply(upgraded)
            with upgraded.read_snapshot() as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='operator_action_effects'").fetchone())
                rows=db.execute('SELECT * FROM external_effects ORDER BY operator_step').fetchall()
                self.assertEqual([row['status'] for row in rows],['uncertain','confirmed','uncertain'])
                self.assertEqual([row['attempts'] for row in rows],[1,1,1])
                self.assertEqual(len({row['request_id'] for row in rows}),3)
                self.assertEqual([row['payload_json'] for row in rows],[json.dumps({'original':i}) for i in range(3)])
                self.assertEqual(json.loads(rows[1]['receipt_json']),{'closure_event':{'id':42}})
                self.assertTrue(all(row['created_at']==2 and row['updated_at']==3 for row in rows))
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
