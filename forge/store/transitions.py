"""Inactive, transactional unit-of-one lifecycle commands.

These commands do not launch workers, inspect GitHub, or choose batch membership.
The caller supplies independently verified evidence. Every accepted mutation and
its event share one SQLite transaction; external effects belong outside it.
"""
from dataclasses import asdict
import hashlib
import json
import time
import uuid

from .models import (
    Candidate, Command, GateCode, IdempotencyConflict, Role, TransitionError,
    VersionConflict,
)

TERMINAL = {"merged", "closed", "dissolved"}
GATE_PRIORITY = {code.value: i for i, code in enumerate(GateCode)}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def manifest_hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _id(request, kind):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"forge:{request}:{kind}"))


def _unit(db, unit_id):
    row = db.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
    if row is None:
        raise TransitionError("unknown unit")
    return dict(row)


def _gates(db, unit_id):
    rows = db.execute(
        "SELECT * FROM unit_gates WHERE unit_id = ? AND resolved_at IS NULL", (unit_id,)
    ).fetchall()
    return sorted((dict(row) for row in rows), key=lambda g: (GATE_PRIORITY[g["code"]], g["created_at"], g["id"]))


def applicable_gates(db, unit, member_id=None, candidate_id=None):
    """All hard gates for this assignment, in the common display order.

    Dependency rows are a fail-closed boundary until the later dependency
    evaluator can validate their observations and input manifests. Even a closed
    prerequisite needs that evaluator; this foundation never infers fulfillment.
    Derived blockers have no gate ID and are not stored as a second authority.
    """
    gates = _gates(db, unit["id"])
    for row in db.execute(
        "SELECT m.id FROM unit_issues m WHERE m.unit_id = ? AND m.left_at IS NULL AND ("
        "EXISTS (SELECT 1 FROM issue_dependencies d WHERE d.issue_id = m.issue_id "
        "AND d.requirements_revision_id = m.requirements_revision_id) OR "
        "EXISTS (SELECT 1 FROM member_dependencies d WHERE d.unit_id = m.unit_id "
        "AND d.member_id = m.id AND d.membership_generation = ?))",
        (unit["id"], unit["membership_generation"]),
    ):
        gates.append({"id": None, "unit_id": unit["id"], "member_id": row["id"], "candidate_id": None,
                      "subject_key": f"member:{row['id']}", "code": "dependency", "created_at": 0.0,
                      "resolved_at": None, "details_json": _json({"reason": "dependency_validation_unavailable"})})
    return sorted((gate for gate in gates
                   if (gate["candidate_id"] is None or gate["candidate_id"] == candidate_id)
                   and (gate["member_id"] is None or gate["member_id"] == member_id
                        or unit["state"] not in {"queued", "building"})),
                  key=lambda g: (GATE_PRIORITY[g["code"]], g["created_at"], g["id"] or g["subject_key"]))


def _charges(db, unit):
    return db.execute(
        "SELECT COALESCE(SUM(CASE b.kind WHEN 'charge' THEN 1 ELSE -1 END), 0) "
        "FROM budget_entries b JOIN attempts a ON a.id = b.attempt_id "
        "WHERE a.unit_id = ? AND a.budget_generation = ?",
        (unit["id"], unit["budget_generation"]),
    ).fetchone()[0]


def project_unit(db, unit_id):
    """Canonical stage/blocker projection; callers must hold a read snapshot."""
    unit = _unit(db, unit_id)
    members = []
    for row in db.execute(
        "SELECT m.*, i.github_state, i.state_reason, i.github_number FROM unit_issues m "
        "JOIN issues i ON i.id = m.issue_id WHERE m.unit_id = ? AND m.left_at IS NULL ORDER BY m.id",
        (unit_id,),
    ):
        member = dict(row)
        gates = applicable_gates(db, unit, member["id"], unit["current_candidate_id"])
        blocker = gates[0] if gates and unit["state"] not in TERMINAL else None
        if member["github_state"] == "closed" and member["state_reason"] == "completed":
            state = "done"
        elif member["github_state"] == "closed" and member["state_reason"] in {"not_planned", "duplicate"}:
            state = "dropped"
        elif unit["state"] == "merged":
            state = "done"
        elif blocker:
            state = "blocked"
        elif unit["state"] == "queued":
            state = "queued"
        else:
            state = "in_unit"
        member.update(current_blocker=blocker, projected_state=state)
        members.append(member)
    gates = applicable_gates(db, unit, candidate_id=unit["current_candidate_id"])
    blocker = gates[0] if gates else None
    if blocker is None and members:
        # An independent runnable/active part keeps the whole unit unblocked.
        runnable = any(m["current_blocker"] is None and m["state"] in {"pending", "building", "failed"} for m in members)
        blocked = [m["current_blocker"] for m in members if m["current_blocker"]]
        if blocked and (unit["state"] not in {"queued", "building"} or not runnable):
            blocker = min(blocked, key=lambda g: (GATE_PRIORITY[g["code"]], g["created_at"], g["id"] or g["subject_key"]))
    unit.update(stage_code=unit["state"], current_blocker=None if unit["state"] in TERMINAL else blocker,
                members=members, charged_failures=_charges(db, unit))
    return unit


class Lifecycle:
    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock

    def _event(self, db, command, kind, payload, *, job_id=None, issue_id=None):
        db.execute(
            "INSERT INTO events(transaction_id,unit_id,issue_id,job_id,kind,at,actor_id,payload_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (command.request_id, command.unit_id, issue_id, job_id, kind, self.clock(), command.actor_id, _json(payload)),
        )

    def _replay(self, db, command, operation, payload):
        row = db.execute("SELECT * FROM decisions WHERE request_id = ?", (command.request_id,)).fetchone()
        if row is None:
            return None
        if (row["unit_id"], row["actor_id"], row["expected_version"], row["command"], row["payload_json"]) != (
            command.unit_id, command.actor_id, command.expected_version, operation, _json(payload)
        ):
            raise IdempotencyConflict("request identity already belongs to a different command")
        if row["status"] != "applied":
            raise TransitionError("request is not an applied command")
        return json.loads(row["result_json"])

    @staticmethod
    def _lifecycle_summary(projected):
        blocker = projected["current_blocker"]
        return {"stage_code": projected["stage_code"], "current_candidate_id": projected["current_candidate_id"],
                "current_blocker": {"id": blocker["id"], "code": blocker["code"], "subject_key": blocker["subject_key"]} if blocker else None}

    def _save(self, db, command, operation, payload, values, *, initial=False, before=None):
        if not initial:
            db.execute("UPDATE units SET version = version + 1, updated_at = ? WHERE id = ?", (self.clock(), command.unit_id))
        projected = project_unit(db, command.unit_id)
        primary = projected["current_blocker"]
        db.execute("UPDATE units SET primary_gate_id = ? WHERE id = ?", (primary["id"] if primary else None, command.unit_id))
        result = {"unit_id": command.unit_id, "version": projected["version"], **values}
        db.execute(
            "INSERT INTO decisions(id,request_id,unit_id,actor_id,expected_version,command,payload_json,status,result_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,'applied',?,?)",
            (_id(command.request_id, "decision"), command.request_id, command.unit_id, command.actor_id,
             command.expected_version, operation, _json(payload), _json(result), self.clock()),
        )
        after = self._lifecycle_summary(projected)
        if before != after:
            self._event(db, command, "lifecycle_changed", {"before": before, "after": after})
        self._event(db, command, "decision_applied", {"command": operation, "version": result["version"], "before": before, "after": after})
        return result

    def _apply(self, command, operation, payload, change):
        if not command.request_id or not command.actor_id or type(command.expected_version) is not int or command.expected_version < 1:
            raise TransitionError("request, actor and positive expected version are required")
        with self.store.write() as db:
            previous = self._replay(db, command, operation, payload)
            if previous is not None:
                return previous
            unit = _unit(db, command.unit_id)
            if unit["version"] != command.expected_version:
                raise VersionConflict("unit changed; read its current version before retrying")
            before = self._lifecycle_summary(project_unit(db, command.unit_id))
            return self._save(db, command, operation, payload, change(db, unit), before=before)

    def admit_issue(self, *, request_id, actor_id, unit_id, issue_id, repository_id, repository_name,
                    issue_number, title, body, requirements, observed_at, base_branch="main"):
        """Admit one observed open issue. Repository readiness is checked by the caller."""
        command = Command(request_id, actor_id, unit_id, 1)
        payload = dict(issue_id=issue_id, repository_id=repository_id, repository_name=repository_name,
                       issue_number=issue_number, title=title, body=body, requirements=requirements,
                       observed_at=observed_at, base_branch=base_branch)
        if not all((request_id, actor_id, unit_id, issue_id, repository_name, base_branch)):
            raise TransitionError("admission identities must be nonempty")
        with self.store.write() as db:
            previous = self._replay(db, command, "admit_issue", payload)
            if previous is not None:
                return previous
            repository = db.execute("SELECT full_name,enabled FROM repositories WHERE id = ?", (repository_id,)).fetchone()
            if repository is None:
                db.execute("INSERT INTO repositories(id,full_name) VALUES(?,?)", (repository_id, repository_name))
            elif repository[0] != repository_name or not repository[1]:
                raise TransitionError("repository identity does not match or is disabled")
            revision_id, member_id = _id(request_id, "revision"), _id(request_id, "member")
            db.execute(
                "INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) "
                "VALUES(?,?,?,'open',?,?)", (issue_id, repository_id, issue_number, revision_id, observed_at),
            )
            db.execute(
                "INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (revision_id, issue_id, title, body, manifest_hash([title, body]),
                                           manifest_hash(requirements), _json(requirements), observed_at),
            )
            db.execute(
                "INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES(?,?,?,'queued',?,?)",
                (unit_id, repository_id, base_branch, self.clock(), self.clock()),
            )
            db.execute(
                "INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) "
                "VALUES(?,?,?,?,?,?,'pending',?)", (member_id, unit_id, issue_id, repository_id, base_branch, revision_id, self.clock()),
            )
            self._event(db, command, "issue_admitted", {"member_id": member_id, "revision_id": revision_id}, issue_id=issue_id)
            return self._save(db, command, "admit_issue", payload, {"member_id": member_id, "revision_id": revision_id}, initial=True)

    def _open_gate(self, db, command, code, details, member_id=None, candidate_id=None):
        code = GateCode(code).value
        if member_id and candidate_id:
            raise TransitionError("a gate has one subject")
        subject = f"member:{member_id}" if member_id else f"candidate:{candidate_id}" if candidate_id else "unit"
        existing = db.execute(
            "SELECT id FROM unit_gates WHERE unit_id = ? AND code = ? AND subject_key = ? AND resolved_at IS NULL",
            (command.unit_id, code, subject),
        ).fetchone()
        if existing:
            raise TransitionError("this subject already has an unresolved gate with that code")
        gate_id = _id(command.request_id, f"gate:{code}:{subject}")
        db.execute(
            "INSERT INTO unit_gates(id,unit_id,candidate_id,member_id,subject_key,code,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (gate_id, command.unit_id, candidate_id, member_id, subject, code, _json(details), self.clock()),
        )
        self._event(db, command, "gate_opened", {"gate_id": gate_id, "code": code, "subject": subject})
        return gate_id

    def open_gate(self, command, code, *, details, member_id=None, candidate_id=None):
        code = GateCode(code).value
        if code in {"operator_pause", "budget_exhausted", "cleanup_pending", "recovery_pending"}:
            raise TransitionError("this gate is managed by its dedicated lifecycle command")
        payload = dict(code=code, details=details, member_id=member_id, candidate_id=candidate_id)
        def change(db, unit):
            self._live(unit)
            if candidate_id and candidate_id != unit["current_candidate_id"]:
                raise TransitionError("gate does not refer to the current candidate")
            return {"gate_id": self._open_gate(db, command, code, details, member_id, candidate_id)}
        return self._apply(command, "open_gate", payload, change)

    def resolve_gate(self, command, gate_id, *, evidence):
        if not evidence:
            raise TransitionError("gate resolution requires evidence")
        def change(db, unit):
            gate = db.execute("SELECT * FROM unit_gates WHERE id = ? AND unit_id = ? AND resolved_at IS NULL", (gate_id, unit["id"])).fetchone()
            if gate is None:
                raise TransitionError("gate is not unresolved on this unit")
            if gate["code"] in {"operator_pause", "budget_exhausted", "cleanup_pending"}:
                raise TransitionError("this gate requires its dedicated lifecycle command")
            if gate["candidate_id"] and gate["candidate_id"] != unit["current_candidate_id"]:
                raise TransitionError("evidence is for an obsolete candidate")
            self._resolve(db, command, gate_id, evidence)
            return {"gate_id": gate_id}
        return self._apply(command, "resolve_gate", {"gate_id": gate_id, "evidence": evidence}, change)

    def _resolve(self, db, command, gate_id, evidence):
        db.execute("UPDATE unit_gates SET resolved_at = ? WHERE id = ?", (self.clock(), gate_id))
        self._event(db, command, "gate_resolved", {"gate_id": gate_id, "evidence": evidence})

    @staticmethod
    def _live(unit):
        if unit["state"] in TERMINAL:
            raise TransitionError("unit is terminal")

    def pause(self, command):
        def change(db, unit):
            self._live(unit)
            gate_id = self._open_gate(db, command, "operator_pause", {})
            self._fence(db, command)
            return {"gate_id": gate_id}
        return self._apply(command, "pause", {}, change)

    def resume(self, command):
        def change(db, unit):
            self._live(unit)
            gate = db.execute("SELECT id FROM unit_gates WHERE unit_id = ? AND code = 'operator_pause' AND resolved_at IS NULL", (unit["id"],)).fetchone()
            if gate is None:
                raise TransitionError("unit has no operator pause")
            self._resolve(db, command, gate[0], {"operator": command.actor_id})
            return {}
        return self._apply(command, "resume", {}, change)

    def _fence(self, db, command):
        db.execute("UPDATE units SET fencing_generation = fencing_generation + 1 WHERE id = ?", (command.unit_id,))
        jobs = db.execute("SELECT id, member_id FROM jobs WHERE unit_id = ? AND cleanup = 0 AND fenced = 0", (command.unit_id,)).fetchall()
        db.execute("UPDATE jobs SET fenced = 1 WHERE unit_id = ? AND cleanup = 0", (command.unit_id,))
        for job in jobs:
            self._ensure_cleanup_gate(db, command, job["id"], job["member_id"])
            self._event(db, command, "attempt_fenced", {}, job_id=job[0])

    def _ensure_cleanup_gate(self, db, command, job_id, member_id):
        subject = f"member:{member_id}" if member_id else "unit"
        exists = db.execute("SELECT 1 FROM unit_gates WHERE unit_id = ? AND code = 'cleanup_pending' AND subject_key = ? AND resolved_at IS NULL", (command.unit_id, subject)).fetchone()
        if not exists:
            self._open_gate(db, command, "cleanup_pending", {"job_id": job_id}, member_id)

    def reserve_job(self, command, role, *, slot, execution_fence, input_manifest, member_id=None, candidate_id=None):
        role = Role(role).value
        payload = dict(role=role, slot=slot, execution_fence=execution_fence, input_manifest=input_manifest,
                       member_id=member_id, candidate_id=candidate_id)
        def change(db, unit):
            self._live(unit)
            expected = {"build": {"queued", "building"}, "review": {"reviewing"}, "audit": {"auditing"}}[role]
            if unit["state"] not in expected:
                raise TransitionError("role is not eligible in this phase")
            if not slot or not execution_fence or not isinstance(input_manifest, dict) or not input_manifest:
                raise TransitionError("reservation requires slot, execution fence and input manifest")
            if role == "build":
                member = db.execute("SELECT * FROM unit_issues WHERE unit_id = ? AND id = ? AND left_at IS NULL", (unit["id"], member_id)).fetchone()
                if member is None or member["state"] not in {"pending", "failed", "building"}:
                    raise TransitionError("build requires a current pending member")
                if not all(isinstance(input_manifest.get(k), str) and input_manifest[k] for k in ("requirements_revision_id", "base_oid", "policy_hash")):
                    raise TransitionError("build manifest requires requirements, base and policy identities")
                if input_manifest.get("requirements_revision_id") != member["requirements_revision_id"]:
                    raise TransitionError("input requirements revision is stale")
                if candidate_id is not None:
                    raise TransitionError("build assignment cannot consume a combined candidate")
            elif member_id is not None or not candidate_id or candidate_id != unit["current_candidate_id"]:
                raise TransitionError("review and audit require the current unit candidate")
            if role != "build":
                identity = db.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
                if input_manifest.get("candidate_id") != candidate_id or any(input_manifest.get(key) != identity[key] for key in ("head_oid", "base_oid", "policy_hash", "requirements_hash", "input_manifest_hash")):
                    raise TransitionError("manifest must pin the exact current candidate")
            if applicable_gates(db, unit, member_id, candidate_id):
                raise TransitionError("assignment is blocked by an applicable gate")
            if _charges(db, unit) >= 3:
                raise TransitionError("unit failure budget is exhausted")
            busy = db.execute(
                "SELECT 1 FROM jobs WHERE unit_id = ? AND cleanup = 0 AND (member_id IS ? OR member_id IS NULL OR ? IS NULL)",
                (unit["id"], member_id, member_id),
            ).fetchone()
            if busy:
                raise TransitionError("prior assignment still requires verified cleanup")
            attempt_id, job_id = _id(command.request_id, "attempt"), _id(command.request_id, "job")
            db.execute(
                "INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (attempt_id, unit["id"], member_id, unit["membership_generation"], unit["fencing_generation"], _json(input_manifest), unit["budget_generation"], self.clock()),
            )
            db.execute(
                "INSERT INTO jobs(id,unit_id,attempt_id,member_id,candidate_id,role,phase,created_at) VALUES(?,?,?,?,?,?,'reserved',?)",
                (job_id, unit["id"], attempt_id, member_id, candidate_id, role, self.clock()),
            )
            db.execute("INSERT INTO job_leases(slot,job_id,execution_fence,acquired_at) VALUES(?,?,?,?)", (slot, job_id, execution_fence, self.clock()))
            if role == "build":
                db.execute("UPDATE units SET state = 'building' WHERE id = ?", (unit["id"],))
                db.execute("UPDATE unit_issues SET state = 'building' WHERE id = ?", (member_id,))
            self._event(db, command, "attempt_reserved", payload, job_id=job_id)
            return {"job_id": job_id, "attempt_id": attempt_id}
        return self._apply(command, "reserve_job", payload, change)

    def _job(self, db, unit, job_id, execution_fence, input_manifest):
        job = db.execute("SELECT j.*, a.assignment_generation, a.membership_generation, a.budget_generation, a.input_manifest_json FROM jobs j JOIN attempts a ON a.id = j.attempt_id WHERE j.id = ? AND j.unit_id = ?", (job_id, unit["id"])).fetchone()
        lease = db.execute("SELECT execution_fence FROM job_leases WHERE job_id = ?", (job_id,)).fetchone()
        if job is None or lease is None or lease[0] != execution_fence:
            raise TransitionError("result does not own the execution lease")
        if job["input_manifest_json"] != _json(input_manifest):
            raise TransitionError("result input manifest differs from its assignment")
        if job["outcome"] is not None:
            raise TransitionError("assignment already has a result")
        current = (not job["fenced"] and job["assignment_generation"] == unit["fencing_generation"]
                   and job["membership_generation"] == unit["membership_generation"]
                   and job["budget_generation"] == unit["budget_generation"]
                   and (job["candidate_id"] is None or job["candidate_id"] == unit["current_candidate_id"]))
        if job["member_id"] is not None:
            member = db.execute("SELECT requirements_revision_id, left_at FROM unit_issues WHERE id = ?", (job["member_id"],)).fetchone()
            current = bool(current and member is not None and member["left_at"] is None
                           and input_manifest.get("requirements_revision_id") == member["requirements_revision_id"])
        return dict(job), current

    def _result(self, db, command, job, evidence, outcome):
        content = _json(evidence).encode()
        db.execute("INSERT INTO job_artifacts(job_id,kind,sequence,media_type,digest,content,size) VALUES(?,'result',0,'application/json',?,?,?)", (job["id"], hashlib.sha256(content).hexdigest(), content, len(content)))
        db.execute("UPDATE jobs SET phase = 'finished', outcome = ? WHERE id = ?", (outcome, job["id"]))
        self._event(db, command, "attempt_finished", {"outcome": outcome}, job_id=job["id"])
        self._ensure_cleanup_gate(db, command, job["id"], job["member_id"])

    def finish_job(self, command, job_id, *, execution_fence, input_manifest, evidence, candidate=None):
        """Accept independently checked build publication or technical pass evidence."""
        if not isinstance(evidence, dict) or not evidence:
            raise TransitionError("result requires structured verified evidence")
        if candidate is not None and not isinstance(candidate, Candidate):
            raise TransitionError("candidate must be a Candidate identity")
        payload = dict(job_id=job_id, execution_fence=execution_fence, input_manifest=input_manifest,
                       evidence=evidence, candidate=asdict(candidate) if candidate else None)
        def change(db, unit):
            job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            if not current:
                self._result(db, command, job, evidence, "stale_success")
                return {"accepted": False}
            if applicable_gates(db, unit, job["member_id"], job["candidate_id"]):
                raise TransitionError("a gate prevents accepting this result")
            expected = {"build": "building", "review": "reviewing", "audit": "auditing"}
            if unit["state"] != expected.get(job["role"]):
                raise TransitionError("result phase is no longer current")
            if job["role"] == "build":
                count = db.execute("SELECT COUNT(*) FROM unit_issues WHERE unit_id = ? AND left_at IS NULL", (unit["id"],)).fetchone()[0]
                if count != 1:
                    raise TransitionError("batch part acceptance belongs to the later batch implementation")
                revision = db.execute("SELECT r.requirements_hash FROM unit_issues m JOIN issue_revisions r ON r.id = m.requirements_revision_id WHERE m.id = ?", (job["member_id"],)).fetchone()
                if (candidate is None or candidate.requirements_hash != revision[0]
                        or candidate.input_manifest_hash != manifest_hash(input_manifest)
                        or candidate.base_oid != input_manifest["base_oid"]
                        or candidate.policy_hash != input_manifest["policy_hash"]
                        or evidence.get("head_oid") != candidate.head_oid
                        or evidence.get("base_oid") != candidate.base_oid
                        or evidence.get("pull_number") != candidate.pull_number):
                    raise TransitionError("publication evidence does not match the assigned candidate")
                candidate_id = _id(command.request_id, "candidate")
                db.execute("INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (candidate_id, unit["id"], candidate.pull_number, candidate.branch, candidate.head_oid, candidate.base_oid, candidate.policy_hash, candidate.requirements_hash, unit["membership_generation"], candidate.input_manifest_hash, self.clock()))
                db.execute("UPDATE units SET current_candidate_id = ? WHERE id = ?", (candidate_id, unit["id"]))
                db.execute("UPDATE unit_issues SET state = 'built' WHERE id = ?", (job["member_id"],))
                self._event(db, command, "candidate_selected", {"candidate_id": candidate_id}, job_id=job_id)
                target = "reviewing"
            else:
                if candidate is not None or evidence.get("candidate_id") != job["candidate_id"] or evidence.get("verdict") != "pass":
                    raise TransitionError("passing verdict must name the exact current candidate")
                target = {"review": "auditing", "audit": "merging"}[job["role"]]
                candidate_id = job["candidate_id"]
            db.execute("INSERT INTO candidate_evidence(id,unit_id,candidate_id,attempt_id,kind,verdict,actor_id,observed_at,details_json) VALUES(?,?,?,?,?,'pass',?,?,?)", (_id(command.request_id, "evidence"), unit["id"], candidate_id, job["attempt_id"], "publication" if job["role"] == "build" else job["role"], command.actor_id, self.clock(), _json(evidence)))
            self._result(db, command, job, evidence, "success")
            db.execute("UPDATE units SET state = ? WHERE id = ?", (target, unit["id"]))
            self._event(db, command, "phase_changed", {"before": unit["state"], "after": target}, job_id=job_id)
            return {"accepted": True, "phase": target}
        return self._apply(command, "finish_job", payload, change)

    def record_failure(self, command, job_id, *, execution_fence, input_manifest, evidence, charge=True):
        if not isinstance(evidence, dict) or not evidence or type(charge) is not bool:
            raise TransitionError("failure requires structured evidence and a boolean charge classification")
        payload = dict(job_id=job_id, execution_fence=execution_fence, input_manifest=input_manifest, evidence=evidence, charge=charge)
        def change(db, unit):
            job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            self._result(db, command, job, evidence, "failure" if current else "stale_failure")
            charged = bool(current and charge)
            if charged:
                charge_id = _id(command.request_id, "charge")
                db.execute("INSERT INTO budget_entries(id,attempt_id,kind,reason_code,created_at) VALUES(?,?,'charge','execution_failure',?)", (charge_id, job["attempt_id"], self.clock()))
                self._event(db, command, "budget_charged", {"charge_id": charge_id}, job_id=job_id)
            if current:
                if job["member_id"]:
                    db.execute("UPDATE unit_issues SET state = 'failed' WHERE id = ?", (job["member_id"],))
                self._open_gate(db, command, "recovery_pending", {"job_id": job_id}, job["member_id"], job["candidate_id"])
                if _charges(db, unit) >= 3:
                    self._open_gate(db, command, "budget_exhausted", {"limit": 3})
                    self._fence(db, command)
            return {"accepted": current, "charged": charged}
        return self._apply(command, "record_failure", payload, change)

    def correct_charge(self, command, charge_id, *, evidence):
        if not isinstance(evidence, dict) or not evidence:
            raise TransitionError("charge correction requires verified evidence")
        def change(db, unit):
            charge = db.execute("SELECT b.*, a.budget_generation FROM budget_entries b JOIN attempts a ON a.id = b.attempt_id WHERE b.id = ? AND a.unit_id = ? AND b.kind = 'charge'", (charge_id, unit["id"])).fetchone()
            if charge is None:
                raise TransitionError("charge does not belong to this unit")
            db.execute("INSERT INTO budget_entries(id,attempt_id,kind,corrects_id,reason_code,created_at) VALUES(?,?,'correction',?,'verified_correction',?)", (_id(command.request_id, "correction"), charge["attempt_id"], charge_id, self.clock()))
            self._event(db, command, "budget_corrected", {"charge_id": charge_id, "evidence": evidence})
            if _charges(db, unit) < 3:
                for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id = ? AND code = 'budget_exhausted' AND resolved_at IS NULL", (unit["id"],)).fetchall():
                    self._resolve(db, command, gate[0], {"charge_correction": charge_id})
            return {}
        return self._apply(command, "correct_charge", {"charge_id": charge_id, "evidence": evidence}, change)

    def verify_cleanup(self, command, job_id, *, execution_fence, evidence):
        if not isinstance(evidence, dict) or evidence.get("process_stopped") is not True or evidence.get("resources_released") is not True:
            raise TransitionError("cleanup needs verified process and resource termination")
        def change(db, unit):
            job = db.execute("SELECT * FROM jobs WHERE id = ? AND unit_id = ? AND cleanup = 0", (job_id, unit["id"])).fetchone()
            lease = db.execute("SELECT execution_fence FROM job_leases WHERE job_id = ?", (job_id,)).fetchone()
            if job is None or lease is None or lease[0] != execution_fence:
                raise TransitionError("cleanup does not own this live lease")
            if not job["fenced"] and job["phase"] != "finished":
                raise TransitionError("active job must be fenced before cleanup")
            db.execute("UPDATE jobs SET fenced = 1, cleanup = 1, phase = 'cleaned' WHERE id = ?", (job_id,))
            db.execute("DELETE FROM job_leases WHERE job_id = ?", (job_id,))
            self._event(db, command, "cleanup_verified", evidence, job_id=job_id)
            remaining = db.execute("SELECT 1 FROM jobs WHERE unit_id = ? AND member_id IS ? AND cleanup = 0", (unit["id"], job["member_id"])).fetchone()
            if not remaining:
                subject = f"member:{job['member_id']}" if job["member_id"] else "unit"
                for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id = ? AND code = 'cleanup_pending' AND subject_key = ? AND resolved_at IS NULL", (unit["id"], subject)).fetchall():
                    self._resolve(db, command, gate[0], {"cleanup_verified": job_id})
            return {}
        return self._apply(command, "verify_cleanup", {"job_id": job_id, "execution_fence": execution_fence, "evidence": evidence}, change)
