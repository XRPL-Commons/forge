"""Bounded decoding, retry identities and conservative raw-log expiry."""
import hashlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zlib

from forge.store.artifacts import (
    ArtifactConflict, ArtifactCorrupt, ArtifactExpired, ArtifactTooLarge,
    Artifacts, MAX_ARTIFACT_SIZE,
)
from forge.store.db import Store


class StoreArtifactTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store.create(folder.name)
        self.artifacts = Artifacts(self.store, clock=lambda: 100.0)
        self.expiry = 100.0 + 30 * 86400
        with self.store.write() as db:
            db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'owner/repo')")
            db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES('u',1,'main','queued',0,0)")
        self.job("j")

    def job(self, job_id, *, cleaned=True, candidate_id=None):
        with self.store.write() as db:
            db.execute("INSERT INTO attempts(id,unit_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) VALUES(?,'u',1,1,'{}',1,0)", ("a-" + job_id,))
            db.execute("INSERT INTO jobs(id,unit_id,attempt_id,candidate_id,role,phase,fenced,cleanup,created_at) VALUES(?,'u',?,?,'review',?,?,?,0)",
                       (job_id, "a-" + job_id, candidate_id, "cleaned" if cleaned else "reserved", int(cleaned), int(cleaned)))

    def candidate(self):
        with self.store.write() as db:
            db.execute("INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES('c','u',1,'branch','head','base','policy','requirements',1,'manifest',0)")
            db.execute("UPDATE units SET current_candidate_id='c' WHERE id='u'")

    def unsafe_artifact(self, *, content, raw_size, digest, codec="zlib", sequence=0):
        with self.store.write() as db:
            db.execute("INSERT INTO job_artifacts(job_id,kind,sequence,media_type,digest,content,size,codec,raw_size) VALUES('j','output',?,'text/plain',?,?,?,?,?)",
                       (sequence, digest, content, len(content), codec, raw_size))

    def test_compression_roundtrip_and_idempotent_retry(self):
        content = b"repeated output\n" * 1000
        first = self.artifacts.put("j", "output", content)
        self.assertEqual(first, self.artifacts.put("j", "output", content))
        result = self.artifacts.read("j", "output")
        self.assertEqual(result.content, content)
        self.assertEqual(result.codec, "zlib")
        self.assertEqual(result.raw_size, len(content))
        with self.assertRaises(ArtifactConflict):
            self.artifacts.put("j", "output", b"different")
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_artifacts").fetchone()[0], 1)

    def test_identity_codec_and_empty_artifact(self):
        self.artifacts.put("j", "summary", b"a")
        self.assertEqual(self.artifacts.read("j", "summary").codec, "identity")
        self.artifacts.put("j", "result", b"")
        self.assertEqual(self.artifacts.read("j", "result", limit=0).content, b"")

    def test_bounds_are_checked_before_inserting_or_fetching_blob(self):
        with self.assertRaises(ArtifactTooLarge):
            self.artifacts.put("j", "output", b"x" * (MAX_ARTIFACT_SIZE + 1))
        self.artifacts.put("j", "output", b"small output")
        statements = []
        connect = self.store._connect_path
        def traced(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        with patch.object(self.store, "_connect_path", side_effect=traced):
            with self.assertRaises(ArtifactTooLarge):
                self.artifacts.read("j", "output", limit=3)
        self.assertFalse(any(sql.startswith("SELECT content ") for sql in statements))
        for limit in (-1, MAX_ARTIFACT_SIZE + 1, True):
            with self.assertRaises(ValueError):
                self.artifacts.read("j", "output", limit=limit)

    def test_decompression_bomb_cannot_exceed_declared_size(self):
        bomb = zlib.compress(b"x" * 10_000_000)
        self.unsafe_artifact(content=bomb, raw_size=8, digest=hashlib.sha256(b"xxxxxxxx").hexdigest())
        with self.assertRaises(ArtifactCorrupt):
            self.artifacts.read("j", "output")

    def test_truncated_trailing_and_concatenated_streams_are_rejected(self):
        raw = b"verified" * 100
        packed = zlib.compress(raw)
        for sequence, content in enumerate((packed[:-1], packed + b"tail", packed + packed)):
            self.unsafe_artifact(content=content, raw_size=len(raw), digest=hashlib.sha256(raw).hexdigest(), sequence=sequence)
            with self.assertRaises(ArtifactCorrupt):
                self.artifacts.read("j", "output", sequence)

    def test_digest_mismatch_is_rejected(self):
        self.unsafe_artifact(content=b"wrong", raw_size=5, digest=hashlib.sha256(b"right").hexdigest(), codec="identity")
        with self.assertRaises(ArtifactCorrupt):
            self.artifacts.read("j", "output")

    def test_expiry_is_distinct_from_missing_and_retries_cannot_resurrect(self):
        self.artifacts.put("j", "output", b"old")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry - 1), 0)
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry), 1)
        with self.assertRaises(ArtifactExpired) as error:
            self.artifacts.read("j", "output")
        self.assertEqual(error.exception.expired_at, self.expiry)
        with self.assertRaises(KeyError):
            self.artifacts.read("j", "output", 1)
        replay = self.artifacts.put("j", "output", b"old")
        self.assertEqual(replay["expired_at"], self.expiry)
        with self.assertRaises(ArtifactExpired):
            self.artifacts.read("j", "output")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry), 0)

    def test_durable_artifacts_and_events_are_retained_forever(self):
        for kind in ("result", "summary", "input_manifest", "launch_input"):
            self.artifacts.put("j", kind, kind.encode())
        with self.store.write() as db:
            db.execute("INSERT INTO events(transaction_id,unit_id,kind,at,payload_json) VALUES('tx','u','unit_created',0,'{}')")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry * 100), 0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        for kind in ("result", "summary", "input_manifest", "launch_input"):
            self.assertEqual(self.artifacts.read("j", kind).content, kind.encode())

    def test_active_job_is_protected(self):
        self.job("active", cleaned=False)
        self.artifacts.put("active", "output", b"still running")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry), 0)

    def test_current_candidate_and_build_publication_evidence_are_protected(self):
        self.candidate()
        self.job("review", candidate_id="c")
        self.artifacts.put("review", "output", b"candidate review")
        self.artifacts.put("j", "output", b"build publication")
        with self.store.write() as db:
            db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,attempt_id,kind,verdict,actor_id,observed_at) VALUES('e','u','c','a-j','publication','pass','operator',0)")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry), 0)

    def test_pending_recovery_or_cleanup_protects_logs(self):
        self.artifacts.put("j", "output", b"needed by recovery")
        with self.store.write() as db:
            db.execute("INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) VALUES('g','u','unit','recovery_pending',0)")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry), 0)
        with self.store.write() as db:
            db.execute("UPDATE unit_gates SET resolved_at=1,resolution='transition' WHERE id='g'")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry), 1)

    def test_durable_evidence_and_idempotency_pins_are_immutable(self):
        self.artifacts.put("j", "ci_transcript", b"historical proof")
        for purpose in ("evidence", "idempotency"):
            self.artifacts.pin("j", "ci_transcript", purpose=purpose, reference_id="historical-record")
            self.artifacts.pin("j", "ci_transcript", purpose=purpose, reference_id="historical-record")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry), 0)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.write() as db:
                db.execute("DELETE FROM artifact_pins")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.write() as db:
                db.execute("UPDATE job_artifacts SET content=NULL,expired_at=?", (self.expiry,))

    def test_merged_candidate_logs_expire_but_pinned_proof_is_retained(self):
        self.candidate()
        self.job("review", candidate_id="c")
        self.artifacts.put("review", "output", b"completed review")
        self.artifacts.put("j", "ci_transcript", b"durable historical proof")
        self.artifacts.pin("j", "ci_transcript", purpose="evidence", reference_id="delivery-proof")
        with self.store.write() as db:
            for phase in ("building", "reviewing", "auditing", "merging"):
                db.execute("UPDATE units SET state=? WHERE id='u'", (phase,))
            db.execute("UPDATE units SET state='merged',merge_sha='merged-sha' WHERE id='u'")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry), 1)
        with self.assertRaises(ArtifactExpired):
            self.artifacts.read("review", "output")
        self.assertEqual(self.artifacts.read("j", "ci_transcript").content, b"durable historical proof")

    def test_pin_can_be_atomic_with_its_reference_and_rejects_expired(self):
        self.artifacts.put("j", "output", b"referenced")
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.write() as db:
                Artifacts.pin_in(db, "j", "output", 0, purpose="evidence", reference_id="record")
                raise RuntimeError("rollback")
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry), 1)
        with self.assertRaises(ArtifactExpired):
            self.artifacts.pin("j", "output", purpose="evidence", reference_id="record")

    def test_pruning_is_one_bounded_batch(self):
        for sequence in range(3):
            self.artifacts.put("j", "output", b"chunk", sequence=sequence)
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry, batch=2), 2)
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry, batch=2), 1)
        self.assertEqual(self.artifacts.prune_raw_logs(now=self.expiry, batch=2), 0)
        for batch in (0, 1001, True):
            with self.assertRaises(ValueError):
                self.artifacts.prune_raw_logs(batch=batch)

    def test_legacy_raw_chunk_without_expiry_metadata_uses_thirty_day_policy(self):
        self.unsafe_artifact(content=b"old", raw_size=3, digest=hashlib.sha256(b"old").hexdigest(), codec="identity")
        self.assertEqual(self.artifacts.prune_raw_logs(now=30 * 86400 - 1), 0)
        self.assertEqual(self.artifacts.prune_raw_logs(now=30 * 86400), 1)
        with self.assertRaises(ArtifactExpired):
            self.artifacts.read("j", "output")

    def test_tombstone_does_not_allow_metadata_edits_or_resurrection(self):
        self.artifacts.put("j", "output", b"immutable")
        for statement in ("UPDATE job_artifacts SET digest='fake'", "DELETE FROM job_artifacts"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.write() as db:
                    db.execute(statement)
        self.artifacts.prune_raw_logs(now=self.expiry)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.write() as db:
                db.execute("UPDATE job_artifacts SET content=?,expired_at=NULL", (b"immutable",))


if __name__ == "__main__":
    unittest.main()
