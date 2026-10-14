"""Bounded artifact storage and explicit, unscheduled raw-log retention."""

from dataclasses import dataclass
import hashlib
import math
import time
import zlib

from .codes import ARTIFACT_KINDS, RAW_LOG_RETENTION_DAYS


MAX_ARTIFACT_SIZE = 1024 * 1024


class ArtifactCorrupt(ValueError):
    """Artifact bytes cannot be trusted."""


class ArtifactTooLarge(ArtifactCorrupt):
    """The artifact exceeds the requested or supported read limit."""


class ArtifactExpired(ValueError):
    """A known artifact was deliberately expired, rather than lost."""

    def __init__(self, expired_at):
        self.expired_at = expired_at
        super().__init__("The raw log has expired under the retention policy")


class ArtifactConflict(ValueError):
    """An immutable artifact identity already contains different data."""


@dataclass(frozen=True)
class Artifact:
    job_id: str
    kind: str
    sequence: int
    media_type: str
    digest: str
    content: bytes
    codec: str = "identity"
    raw_size: int = 0


def _identity(job_id, kind, sequence):
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must be nonempty")
    if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
        raise ValueError("unknown artifact kind")
    if type(sequence) is not int or sequence < 0:
        raise ValueError("sequence must be a nonnegative integer")


def read_in(db, job_id, kind, sequence=0, *, limit=MAX_ARTIFACT_SIZE):
    """Read and verify an artifact within an existing snapshot or transaction."""
    _identity(job_id, kind, sequence)
    if type(limit) is not int or not 0 <= limit <= MAX_ARTIFACT_SIZE:
        raise ValueError("limit must be an integer within the supported chunk size")
    metadata = db.execute(
        "SELECT media_type,digest,size,COALESCE(raw_size,size) AS raw_size,codec,expired_at,"
        "length(content) AS stored_length FROM job_artifacts WHERE job_id=? AND kind=? AND sequence=?",
        (job_id, kind, sequence),
    ).fetchone()
    if metadata is None:
        raise KeyError((job_id, kind, sequence))
    if metadata["expired_at"] is not None:
        raise ArtifactExpired(metadata["expired_at"])
    if (not 0 <= metadata["size"] <= MAX_ARTIFACT_SIZE
            or not 0 <= metadata["raw_size"] <= MAX_ARTIFACT_SIZE):
        raise ArtifactCorrupt("artifact size metadata exceeds the supported size")
    if metadata["raw_size"] > limit:
        raise ArtifactTooLarge("artifact exceeds the requested read limit")
    if metadata["stored_length"] != metadata["size"]:
        raise ArtifactCorrupt("artifact stored size does not match its metadata")
    content = db.execute(
        "SELECT content FROM job_artifacts WHERE job_id=? AND kind=? AND sequence=?", (job_id, kind, sequence),
    ).fetchone()[0]
    if metadata["codec"] == "zlib":
        inflater = zlib.decompressobj()
        try:
            content = inflater.decompress(content, metadata["raw_size"] + 1)
        except zlib.error as error:
            raise ArtifactCorrupt("artifact compression stream is corrupt") from error
        if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise ArtifactCorrupt("artifact compression stream is incomplete or exceeds its declared size")
    elif metadata["codec"] != "identity":
        raise ArtifactCorrupt("artifact codec is unsupported")
    if (len(content) != metadata["raw_size"]
            or hashlib.sha256(content).hexdigest() != metadata["digest"]):
        raise ArtifactCorrupt("artifact raw size or digest does not match")
    return Artifact(job_id, kind, sequence, metadata["media_type"], metadata["digest"],
                    content, metadata["codec"], metadata["raw_size"])



class Artifacts:
    """One artifact chunk per explicit identity; retries never append duplicates.

    Producers must pin any raw-log chunk needed as durable evidence or as an
    idempotency reference in the same transaction that records that reference.
    Existing structured results, summaries and input manifests never expire.
    """

    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock

    def put(self, job_id, kind, data, *, media_type="text/plain", sequence=0):
        _identity(job_id, kind, sequence)
        if not isinstance(data, bytes):
            raise ValueError("artifact data must be bytes")
        if len(data) > MAX_ARTIFACT_SIZE:
            raise ArtifactTooLarge("artifact chunk exceeds the supported size")
        if not isinstance(media_type, str) or not media_type or len(media_type) > 200:
            raise ValueError("media_type must be a nonempty short string")
        digest = hashlib.sha256(data).hexdigest()
        packed = zlib.compress(data, 6)
        codec, content = ("zlib", packed) if len(packed) < len(data) else ("identity", data)
        now = self.clock()
        expires = now + RAW_LOG_RETENTION_DAYS * 86400 if ARTIFACT_KINDS[kind] == "raw" else None
        with self.store.write() as db:
            previous = db.execute(
                "SELECT media_type,digest,COALESCE(raw_size,size) AS raw_size,expired_at "
                "FROM job_artifacts WHERE job_id=? AND kind=? AND sequence=?", (job_id, kind, sequence),
            ).fetchone()
            if previous is not None:
                if (previous["media_type"], previous["digest"], previous["raw_size"]) != (media_type, digest, len(data)):
                    raise ArtifactConflict("artifact identity already contains different data")
                return {"job_id": job_id, "kind": kind, "sequence": sequence,
                        "digest": digest, "raw_size": len(data), "expired_at": previous["expired_at"]}
            db.execute(
                "INSERT INTO job_artifacts(job_id,kind,sequence,media_type,codec,raw_size,digest,content,size,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, kind, sequence, media_type, codec, len(data), digest, content, len(content), now, expires),
            )
            return {"job_id": job_id, "kind": kind, "sequence": sequence,
                    "digest": digest, "raw_size": len(data), "expired_at": None}

    def read(self, job_id, kind, sequence=0, *, limit=MAX_ARTIFACT_SIZE):
        with self.store.read_snapshot() as db:
            return read_in(db, job_id, kind, sequence, limit=limit)

    @staticmethod
    def pin_in(db, job_id, kind, sequence, *, purpose, reference_id):
        """Retain a raw chunk atomically with the caller's durable reference."""
        _identity(job_id, kind, sequence)
        if (not isinstance(purpose, str) or not 1 <= len(purpose) <= 200
                or not isinstance(reference_id, str) or not 1 <= len(reference_id) <= 1000):
            raise ValueError("pin purpose and reference identity are required")
        row = db.execute("SELECT expired_at FROM job_artifacts WHERE job_id=? AND kind=? AND sequence=?",
                         (job_id, kind, sequence)).fetchone()
        if row is None:
            raise KeyError((job_id, kind, sequence))
        if row[0] is not None:
            raise ArtifactExpired(row[0])
        db.execute("INSERT INTO artifact_pins(job_id,kind,sequence,purpose,reference_id) VALUES(?,?,?,?,?) "
                   "ON CONFLICT DO NOTHING", (job_id, kind, sequence, purpose, reference_id))

    def pin(self, job_id, kind, sequence=0, *, purpose, reference_id):
        with self.store.write() as db:
            self.pin_in(db, job_id, kind, sequence, purpose=purpose, reference_id=reference_id)

    def prune_raw_logs(self, *, now=None, batch=500):
        """Tombstone one bounded batch of eligible logs after 30 days.

        The explicit SQLite supervisor schedules bounded pruning. Active work, current-candidate evidence,
        recovery/cleanup and explicitly pinned historical references are retained.
        """
        now = self.clock() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("now must be a finite nonnegative timestamp")
        if type(batch) is not int or not 1 <= batch <= 1000:
            raise ValueError("batch must be an integer between 1 and 1000")
        raw = tuple(kind for kind, policy in ARTIFACT_KINDS.items() if policy == "raw")
        marks = ",".join("?" for _ in raw)
        with self.store.write() as db:
            rows = db.execute(
                "SELECT a.rowid FROM job_artifacts a JOIN jobs j ON j.id=a.job_id JOIN units u ON u.id=j.unit_id "
                f"WHERE a.kind IN ({marks}) AND a.expired_at IS NULL AND a.content IS NOT NULL "
                "AND a.created_at <= ? AND (a.expires_at IS NULL OR a.expires_at <= ?) AND j.cleanup=1 "
                "AND NOT EXISTS (SELECT 1 FROM artifact_pins p WHERE p.job_id=a.job_id AND p.kind=a.kind AND p.sequence=a.sequence) "
                "AND NOT EXISTS (SELECT 1 FROM job_leases l WHERE l.job_id=j.id) "
                "AND (u.state IN ('merged','closed','dissolved') OR ((j.candidate_id IS NULL OR j.candidate_id IS NOT u.current_candidate_id) "
                "AND NOT EXISTS (SELECT 1 FROM candidate_evidence e WHERE e.attempt_id=j.attempt_id AND e.candidate_id=u.current_candidate_id))) "
                "AND NOT EXISTS (SELECT 1 FROM unit_gates g WHERE g.unit_id=u.id AND g.resolved_at IS NULL "
                "AND g.code IN ('recovery_pending','cleanup_pending','result_pending')) ORDER BY a.created_at,a.rowid LIMIT ?",
                (*raw, now - RAW_LOG_RETENTION_DAYS * 86400, now, batch),
            ).fetchall()
            for row in rows:
                db.execute("UPDATE job_artifacts SET content=NULL,expired_at=? WHERE rowid=?", (now, row[0]))
            from .auxiliary import prune_logs_in
            return len(rows) + prune_logs_in(db, now=now, batch=batch-len(rows))
