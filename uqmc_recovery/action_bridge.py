"""Default-disabled handoff protocol; this module never calls a connector.

AGENT_ASSISTED mode requires separately recorded authorization and dispatch review.
It emits one bounded ticket for an external authorized agent, with a documented
read/write race window. Local evidence is not cryptographically authenticated:
the controller must import provenance from trusted user/tool history honestly.
Strict complete metadata and atomic CAS below describe the fixture contract only;
they do not assert those capabilities exist in the discovered Drive connector.
An emitted local ticket is not a server idempotency key and cannot stop a human
from manually repeating a separate tool call. Lost tickets are never reissued.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import re
import uuid

from .engine import ACTION_STATES, Engine, SUCCESS, now, parsed
from .model import Approval, FOLDER, Operation, Plan, SafetyError, WITHDRAWN_ID, digest, semantic, snapshot


_BRIDGE = "TEST_ONLY_HANDOFF"
_AGENT_BRIDGE = "AGENT_ASSISTED_HANDOFF"
AGENT_TOOL = "mcp__codex_apps__google_drive_update_file"
AGENT_LIMITATIONS = {"NO_ATOMIC_PRECONDITION", "READ_WRITE_RACE", "NO_EXACTLY_ONCE",
                     "UNKNOWN_PROVIDER_FIELDS", "UNCERTAIN_DISPATCH_NEVER_RETRIED"}
_TERMINAL = {"RECONCILED_AT_TARGET", "FAILED_NOT_APPLIED"}
_STABLE_ID = re.compile(r"[A-Za-z0-9_-]+")


def _id(value):
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value) or value == "root":
        raise SafetyError("STABLE_FILE_ID_REQUIRED")
    return value


def _plan(body):
    return Plan(body["run_id"], body["batch_id"], body["proposal_revision"],
                tuple(Operation(**op) for op in body["operations"]), tuple(body.get("excluded_ids", ())))


class _FixtureEvidenceAdapter:
    is_mock = True

    def __init__(self, preflight):
        self.check = preflight

    def preflight(self, *args):
        return copy.deepcopy(self.check)


class ActionBridge:
    """Persist an exact simulation request before a separately invoked fake tool.

    ``mark_attempt`` needs a fresh snapshot and complete repeated preflight. The
    fixture's concurrency proof binds their exact contents; a real connector
    implementation would need a separately reviewed limitations/unlock policy.
    """

    def __init__(self, journal, mode="DISABLED", fault=None, controller_id=None):
        if mode not in {"DISABLED", "TEST_ONLY", "AGENT_ASSISTED"}:
            raise SafetyError("LIVE_EXECUTION_DISABLED")
        self.journal, self.mode = journal, mode
        self.controller_id = controller_id
        if mode == "AGENT_ASSISTED" and (not isinstance(controller_id, str) or not controller_id):
            raise SafetyError("AGENT_CONTROLLER_REQUIRED")
        self.fault = fault or (lambda point: None)

    def _mode(self, dispatch=False):
        if self.mode not in {"DISABLED", "TEST_ONLY", "AGENT_ASSISTED"} or (dispatch and self.mode == "DISABLED"):
            raise SafetyError("LIVE_EXECUTION_DISABLED")

    def _emit(self, kind, op_id, payload, attempt=1):
        self.fault("before_" + kind)
        record = self.journal.append(kind, operation_id=op_id, attempt=attempt, payload=payload)
        self.fault("after_" + kind)
        return record

    def _events(self, envelope_hash):
        return [r for r in self.journal.records() if r["payload"].get("envelope_hash") == envelope_hash]

    def _latest(self, envelope_hash):
        rows = [r for r in self._events(envelope_hash) if r["kind"] in ACTION_STATES]
        return rows[-1] if rows else None

    def _load(self, envelope_hash):
        prepared = [r for r in self._events(envelope_hash) if r["kind"] == "PREPARED"
                    and r["payload"].get("bridge") in {_BRIDGE, _AGENT_BRIDGE}]
        if len(prepared) != 1:
            raise SafetyError("HANDOFF_UNKNOWN_OR_AMBIGUOUS")
        event = prepared[0]
        envelope = self.journal.read_evidence(event["payload"]["envelope_ref"])
        body = {k: v for k, v in envelope.items() if k != "envelope_hash"}
        if envelope.get("envelope_hash") != envelope_hash or digest(body) != envelope_hash:
            raise SafetyError("HANDOFF_ENVELOPE_HASH_MISMATCH")
        for key in ("attempt_key", "proposal_hash", "file_id", "approval_id"):
            if event["payload"].get(key) != envelope.get(key):
                raise SafetyError("HANDOFF_ENVELOPE_BINDING_MISMATCH")
        return envelope, event

    def _frozen(self, plan, op_id):
        if self.mode == "AGENT_ASSISTED":
            from .agent_profile import validate_agent_plan
            validate_agent_plan(plan)
        else:
            plan.validate()
        records = [r for r in self.journal.records() if r["kind"] == "DRAFTED"
                   and r["payload"].get("run_id") == plan.run_id]
        if len(records) != 1 or records[0]["payload"].get("proposal_hash") != plan.proposal_hash:
            raise SafetyError("FROZEN_PLAN_MISMATCH")
        frozen = self.journal.read_evidence(records[0]["payload"]["plan_ref"])
        if digest(frozen) != plan.proposal_hash or digest(plan.body()) != digest(frozen):
            raise SafetyError("FROZEN_PLAN_EVIDENCE_MISMATCH")
        matches = [op for op in plan.operations if op.operation_id == op_id]
        if len(matches) != 1:
            raise SafetyError("OPERATION_NOT_IN_FROZEN_PLAN")
        return matches[0], records[0]["payload"]["plan_ref"]

    def _validate(self, plan, op_id, approval, before, preflight):
        if self.mode == "AGENT_ASSISTED":
            return self._validate_agent(plan, op_id, approval, before, preflight)
        op, plan_ref = self._frozen(plan, op_id)
        engine = Engine(self.journal, _FixtureEvidenceAdapter(preflight), fault=self.fault)
        excluded = engine._guard_plan(plan)
        for dependency in op.depends_on:
            state = engine.latest(dependency)
            if not state or state["kind"] not in SUCCESS:
                raise SafetyError("HANDOFF_DEPENDENCY_PENDING")
        before = snapshot(before)
        # A verified same-file dependency may advance version/time. Only that
        # recorded dependency chain may refresh the frozen semantic baseline.
        if before != engine._baseline(op):
            raise SafetyError("HANDOFF_BASELINE_DRIFT")
        _id(op.file_id)
        if before["id"] != op.file_id:
            raise SafetyError("ID_MISMATCH")
        if not isinstance(preflight, dict):
            raise SafetyError("INCOMPLETE_PREFLIGHT")
        # An explicit TEST_ONLY approval is still checked for expiry, exact
        # scope, revision/hash, actor, and revocation. No approval is fabricated.
        approval_ref = engine._approval(plan, approval)
        check, expected = engine._preflight(op, before, excluded, approval)
        if check.get("ancestors_complete") is not True or not isinstance(check.get("ancestor_ids"), list):
            raise SafetyError("FIXTURE_ANCESTRY_INCOMPLETE")
        scope = {op.file_id} | set(before["parents"]) | set(op.target.get("parents", []))
        scope |= set(check["ancestor_ids"]) | set(check.get("descendant_ids", []))
        scope |= {member["id"] for member in before["subtree"].get("members", [])}
        for key, parent in check["parents"].items():
            if parent["id"] != key:
                raise SafetyError("PARENT_ID_MISMATCH")
            scope |= set(parent["parents"])
        for value in scope:
            _id(value)
        if scope & excluded:
            raise SafetyError("EXCLUDED")
        concurrency = check.get("concurrency", {})
        if concurrency.get("mechanism") != "FIXTURE_ATOMIC_CAS" or concurrency.get("enforced") is not True:
            raise SafetyError("FIXTURE_CONCURRENCY_PROOF_REQUIRED")
        proof = self.journal.read_evidence(concurrency.get("evidence_ref"))
        safety = {k: v for k, v in check.items() if k != "concurrency"}
        expected_proof = {"mode": "TEST_ONLY", "mechanism": "FIXTURE_ATOMIC_CAS", "enforced": True,
                          "file_id": op.file_id, "before_hash": digest(before), "safety_hash": digest(safety)}
        if proof != expected_proof:
            raise SafetyError("FIXTURE_CONCURRENCY_BINDING_MISMATCH")
        if semantic(before) == semantic(expected):
            raise SafetyError("HANDOFF_NO_CHANGE_REQUIRED")
        return op, before, expected, check, sorted(scope), plan_ref, approval_ref

    def _agent_approval(self, plan, approval):
        if approval.mode != "AGENT_ASSISTED":
            raise SafetyError("AGENT_APPROVAL_MODE_REQUIRED")
        if any(getattr(plan, k) != getattr(approval, k) for k in
               ("run_id", "batch_id", "proposal_revision", "proposal_hash")):
            raise SafetyError("STALE_APPROVAL")
        if (set(approval.operation_ids) != {op.operation_id for op in plan.operations}
                or len(set(approval.operation_ids)) != len(approval.operation_ids)):
            raise SafetyError("APPROVAL_SCOPE")
        if not approval.actor or not approval.approval_id:
            raise SafetyError("MISSING_APPROVAL_IDENTITY")
        if not parsed(approval.approved_at) <= datetime.now(timezone.utc) < parsed(approval.expires_at):
            raise SafetyError("APPROVAL_EXPIRED_OR_FUTURE")
        if any(r["kind"] == "REVOKED" and r["payload"].get("approval_id") == approval.approval_id
               for r in self.journal.records()):
            raise SafetyError("APPROVAL_REVOKED")

    def _provenance(self, reference, kind):
        evidence = self.journal.read_evidence(reference)
        if not isinstance(evidence, dict) or evidence.get("kind") != kind:
            raise SafetyError("AGENT_PROVENANCE_REQUIRED")
        return evidence

    def authorize_agent_dispatch(self, plan, approval, authorization_evidence, enable_evidence):
        """Record supplied human authorization plus a separate controller review.

        This function does not manufacture authority or dispatch anything. The
        caller must bind actual trusted conversation/tool evidence, never a
        proposal checkbox, an inferred instruction, or a generated approval.
        """
        with self.journal.locked():
            self._mode(dispatch=True)
            if self.mode != "AGENT_ASSISTED":
                raise SafetyError("AGENT_MODE_REQUIRED")
            self._frozen(plan, plan.operations[0].operation_id)
            self._agent_approval(plan, approval)
            authorization = copy.deepcopy(authorization_evidence)
            enable = copy.deepcopy(enable_evidence)
            if (authorization.get("kind") != "EXPLICIT_BATCH_AUTHORIZATION"
                    or authorization.get("authority") != "human_user" or authorization.get("authorized") is not True
                    or authorization.get("actor") != approval.actor
                    or authorization.get("approval_hash") != digest(approval.body())
                    or authorization.get("proposal_hash") != plan.proposal_hash
                    or not AGENT_LIMITATIONS <= set(authorization.get("limitations_acknowledged", []))):
                raise SafetyError("AGENT_AUTHORIZATION_BINDING_REQUIRED")
            provenance = self._provenance(authorization.get("provenance_ref"), "TRUSTED_USER_INSTRUCTION")
            if not isinstance(provenance.get("text"), str) or not provenance["text"].strip():
                raise SafetyError("AGENT_USER_INSTRUCTION_REQUIRED")
            exposure = self._provenance(authorization.get("exposure_review_ref"), "SCOPED_EXPOSURE_REVIEW")
            if (exposure.get("proposal_hash") != plan.proposal_hash
                    or set(exposure.get("operation_ids", [])) != set(approval.operation_ids)
                    or exposure.get("warning_presented") is not True or exposure.get("approved") is not True
                    or not isinstance(exposure.get("newly_exposed_principals"), list)
                    or type(exposure.get("unknown_exposure")) is not bool
                    or authorization.get("access_policy") != "SCOPED_EXPOSURE_ACCEPTED"):
                raise SafetyError("AGENT_SCOPED_EXPOSURE_APPROVAL_REQUIRED")
            if (enable.get("kind") != "DISPATCH_ENABLE_REVIEW" or enable.get("controller_id") != self.controller_id
                    or enable.get("proposal_hash") != plan.proposal_hash or enable.get("approval_id") != approval.approval_id
                    or enable.get("authorization_hash") != digest(authorization)
                    or enable.get("manifest_verified") is not True or not enable.get("reviewer")):
                raise SafetyError("AGENT_DISPATCH_REVIEW_REQUIRED")
            self._provenance(enable.get("provenance_ref"), "TRUSTED_CONTROLLER_REVIEW")
            if not parsed(enable["reviewed_at"]) <= datetime.now(timezone.utc) < parsed(enable["expires_at"]) <= parsed(approval.expires_at):
                raise SafetyError("AGENT_DISPATCH_REVIEW_EXPIRED")
            # Only one controller may own the store until explicitly disabled.
            controls = [r for r in self.journal.records() if r["kind"] in
                        {"HANDOFF_DISPATCH_ENABLED", "HANDOFF_DISPATCH_DISABLED"}]
            if controls and controls[-1]["kind"] == "HANDOFF_DISPATCH_ENABLED" and controls[-1]["payload"]["controller_id"] != self.controller_id:
                raise SafetyError("AGENT_OTHER_CONTROLLER_ENABLED")
            approval_ref = self.journal.evidence(approval.body())
            authorization_ref = self.journal.evidence(authorization)
            enable_ref = self.journal.evidence(enable)
            prior = [r for r in self.journal.records() if r["kind"] == "APPROVED"
                     and r["payload"].get("approval_id") == approval.approval_id]
            if prior and prior[-1]["payload"]["approval_ref"] != approval_ref:
                raise SafetyError("APPROVAL_ID_REUSED")
            common = {"mode": "AGENT_ASSISTED", "run_id": plan.run_id, "batch_id": plan.batch_id,
                      "proposal_revision": plan.proposal_revision, "proposal_hash": plan.proposal_hash,
                      "approval_id": approval.approval_id, "approval_ref": approval_ref,
                      "controller_id": self.controller_id, "actor": approval.actor}
            if not prior:
                self._emit("APPROVED", None, common, 0)
            auth_payload = common | {"authorization_ref": authorization_ref}
            if not any(r["kind"] == "HANDOFF_AUTHORIZED" and r["payload"] == auth_payload for r in self.journal.records()):
                self._emit("HANDOFF_AUTHORIZED", None, auth_payload, 0)
            enable_payload = auth_payload | {"enable_ref": enable_ref}
            if not controls or controls[-1]["kind"] != "HANDOFF_DISPATCH_ENABLED" or controls[-1]["payload"] != enable_payload:
                self._emit("HANDOFF_DISPATCH_ENABLED", None, enable_payload, 0)
            return {"authorization_ref": authorization_ref, "enable_ref": enable_ref,
                    "approval_ref": approval_ref, "controller_id": self.controller_id,
                    "agent_handoff_enabled": True, "external_tool_calls": 0}

    def disable_agent_dispatch(self, reason_code="HOLD"):
        with self.journal.locked():
            return self._emit("HANDOFF_DISPATCH_DISABLED", None,
                {"controller_id": self.controller_id, "reason_code": reason_code}, 0)

    def _agent_authority(self, plan, approval):
        self._agent_approval(plan, approval)
        controls = [r for r in self.journal.records() if r["kind"] in
                    {"HANDOFF_DISPATCH_ENABLED", "HANDOFF_DISPATCH_DISABLED"}]
        if not controls or controls[-1]["kind"] != "HANDOFF_DISPATCH_ENABLED":
            raise SafetyError("AGENT_DISPATCH_NOT_ENABLED")
        p = controls[-1]["payload"]
        if (p["controller_id"] != self.controller_id or p["proposal_hash"] != plan.proposal_hash
                or p["approval_id"] != approval.approval_id
                or self.journal.read_evidence(p["approval_ref"]) != approval.body()):
            # JSON arrays and dataclass tuples are equivalent canonical forms.
            if (p["controller_id"] != self.controller_id or p["proposal_hash"] != plan.proposal_hash
                    or p["approval_id"] != approval.approval_id
                    or digest(self.journal.read_evidence(p["approval_ref"])) != digest(approval.body())):
                raise SafetyError("AGENT_ENABLE_SCOPE_MISMATCH")
        enable = self.journal.read_evidence(p["enable_ref"])
        authorization = self.journal.read_evidence(p["authorization_ref"])
        if (enable["authorization_hash"] != digest(authorization)
                or authorization["approval_hash"] != digest(approval.body())
                or datetime.now(timezone.utc) >= parsed(enable["expires_at"])):
            raise SafetyError("AGENT_ENABLE_STALE")
        return p

    def _agent_read(self, value, proof, minimum_time=None):
        from .agent_profile import agent_snapshot
        current = agent_snapshot(value)
        if not isinstance(proof, dict) or proof.get("kind") != "AGENT_CONNECTOR_READ":
            raise SafetyError("AGENT_FRESH_READ_REQUIRED")
        at = parsed(proof["observed_at"])
        clock = datetime.now(timezone.utc)
        if not clock - timedelta(seconds=60) <= at <= clock or (minimum_time and at < parsed(minimum_time)):
            raise SafetyError("AGENT_READ_STALE")
        if self._agent_exact(self.journal.read_evidence(proof["snapshot_ref"])) != self._agent_exact(current):
            raise SafetyError("AGENT_READ_SNAPSHOT_MISMATCH")
        raw = self.journal.read_evidence(proof["receipt_ref"])
        for _ in range(4):
            if isinstance(raw, dict) and "raw" in raw:
                raw = raw["raw"]
            elif isinstance(raw, dict) and "structuredContent" in raw:
                if raw.get("isError") is True:
                    raise SafetyError("AGENT_READ_ERROR")
                raw = raw["structuredContent"]
            else:
                break
        if not isinstance(raw, dict):
            raise SafetyError("AGENT_READ_RECEIPT_INVALID")
        actual = {"id": raw.get("id"), "name": raw.get("name", raw.get("title")),
                  "mime_type": raw.get("mime_type", raw.get("mimeType")),
                  "parents": raw.get("parents", raw.get("parent_ids"))}
        expected = {k: current[k] for k in actual}
        if isinstance(actual["parents"], list):
            actual["parents"] = sorted(actual["parents"])
        if actual != expected:
            raise SafetyError("AGENT_READ_IDENTITY_MISMATCH")
        aliases = {"version": ("version",), "modified_time": ("modified_time", "modifiedTime"),
                   "trashed": ("trashed",), "permissions": ("permissions",), "owners": ("owners",),
                   "drive_id": ("drive_id", "driveId"), "capabilities": ("capabilities",),
                   "shortcut_details": ("shortcut_details", "shortcutDetails"),
                   "md5_checksum": ("md5_checksum", "md5Checksum"), "size": ("size",), "access": ("access",)}
        for field, keys in aliases.items():
            if current.get(field) is None:
                continue
            found = [raw[key] for key in keys if key in raw]
            if not found or any(value != current[field] for value in found):
                raise SafetyError("AGENT_OPTIONAL_FIELD_UNPROVEN")
        return current

    @staticmethod
    def _agent_exact(value):
        from .agent_profile import agent_snapshot
        reporting = {"observation_profile", "metadata_unavailable", "observed_at", "observed_at_utc",
                     "metadata_verified_at_utc", "missing_fields", "unavailable_fields"}
        return {key: val for key, val in agent_snapshot(value).items() if key not in reporting}

    @staticmethod
    def _agent_safety(check):
        """Compare safety observations separately from fresh receipt timestamps."""
        from .agent_profile import agent_semantic
        out = {k: copy.deepcopy(v) for k, v in check.items()
               if k not in {"source_read", "parent_reads", "collision_review_ref", "membership_ref"}}
        out["parents"] = {key: agent_semantic(value) for key, value in out["parents"].items()}
        out["expected_after"] = agent_semantic(out["expected_after"])
        return out

    def _agent_checks(self, op, before, check, plan, approval, *, source=None, minimum_time=None, controller_id=None):
        from .agent_profile import agent_snapshot, agent_semantic
        if not isinstance(check, dict) or check.get("complete") is not True or check.get("no_cycle") is not True:
            raise SafetyError("AGENT_PREFLIGHT_INCOMPLETE")
        self._agent_read(source or before, check.get("source_read"), minimum_time)
        required = set(before["parents"]) | set(op.target.get("parents", []))
        if required != set(check.get("parents", {})) or required != set(check.get("parent_reads", {})):
            raise SafetyError("AGENT_PARENTS_INCOMPLETE")
        for file_id, value in check["parents"].items():
            parent = self._agent_read(value, check["parent_reads"][file_id], minimum_time)
            if parent["id"] != file_id or parent["mime_type"] != FOLDER:
                raise SafetyError("INVALID_PARENT")
        collisions = check.get("collision_ids")
        if not isinstance(collisions, list) or not set(collisions) <= set(getattr(op, "allowed_collision_ids", ())):
            raise SafetyError("UNAPPROVED_NAME_COLLISION")
        collision = self._provenance(check.get("collision_review_ref"), "COLLISION_REVIEW")
        clock = datetime.now(timezone.utc)
        collision_time = parsed(collision["observed_at"])
        if not clock - timedelta(seconds=60) <= collision_time <= clock or (minimum_time and collision_time < parsed(minimum_time)):
            raise SafetyError("AGENT_COLLISION_REVIEW_STALE")
        intended = op.intended(before)
        if (collision.get("proposal_hash") != plan.proposal_hash or collision.get("operation_id") != op.operation_id
                or collision.get("complete") is not True or collision.get("target_name") != intended["name"]
                or sorted(collision.get("target_parent_ids", [])) != sorted(intended["parents"])
                or sorted(collision.get("collision_ids", [])) != sorted(collisions)):
            raise SafetyError("AGENT_COLLISION_REVIEW_MISMATCH")
        if before["mime_type"] == FOLDER:
            members = sorted(m["id"] for m in (source or before)["subtree"]["members"])
            membership = self._provenance(check.get("membership_ref"), "COMPLETE_MEMBERSHIP_READ")
            if (membership.get("root_id") != op.file_id or membership.get("complete") is not True
                    or membership.get("pagination_complete") is not True or sorted(membership.get("member_ids", [])) != members):
                raise SafetyError("AGENT_MEMBERSHIP_INCOMPLETE")
            membership_time = parsed(membership["observed_at"])
            if not clock - timedelta(seconds=60) <= membership_time <= clock or (minimum_time and membership_time < parsed(minimum_time)):
                raise SafetyError("AGENT_MEMBERSHIP_STALE")
        expected = agent_snapshot(check.get("expected_after"))
        if any(expected[key] != intended[key] for key in ("id", "name", "mime_type", "parents")):
            raise SafetyError("PREFLIGHT_SCOPE_MISMATCH")
        if before.get("subtree") != expected.get("subtree"):
            raise SafetyError("AGENT_SUBTREE_CHANGE_NOT_ALLOWED")
        stable = {"owners", "drive_id", "shortcut_details", "md5_checksum", "size"}
        if op.action == "RENAME":
            stable |= {"permissions", "access", "capabilities"}
        if any(before.get(key) != expected.get(key) for key in stable):
            raise SafetyError("AGENT_UNAPPROVED_EXPECTED_FIELD_CHANGE")
        concurrency = check.get("concurrency", {})
        if (concurrency.get("mechanism") != "SINGLE_CONTROLLER_FRESH_READS"
                or concurrency.get("controller_id") != (controller_id or self.controller_id)
                or concurrency.get("atomic_precondition") is not False
                or concurrency.get("race_window_acknowledged") is not True):
            raise SafetyError("AGENT_RACE_ACKNOWLEDGEMENT_REQUIRED")
        return expected

    def _validate_agent(self, plan, op_id, approval, before, preflight):
        from .agent_profile import agent_snapshot, agent_semantic
        op, plan_ref = self._frozen(plan, op_id)
        authority = self._agent_authority(plan, approval)
        records = self.journal.records()
        excluded = {WITHDRAWN_ID} | set(plan.excluded_ids) | {r["payload"]["file_id"] for r in records if r["kind"] == "EXCLUDED"}
        excluded_refs = {r["payload"]["proposal_ref"] for r in records if r["kind"] == "PROPOSAL_EXCLUDED"}
        if set(op.proposal_refs) & excluded_refs:
            raise SafetyError("PROPOSAL_EXCLUDED")
        baseline = agent_snapshot(op.before)
        dependency_states = []
        todo, visited = list(op.depends_on), set()
        while todo:
            dep = todo.pop()
            if dep in visited:
                continue
            visited.add(dep)
            rows = [r for r in records if r["operation_id"] == dep and r["kind"] in ACTION_STATES]
            if not rows or rows[-1]["kind"] not in SUCCESS:
                raise SafetyError("HANDOFF_DEPENDENCY_PENDING")
            dependency_states.append(rows[-1])
            todo.extend(rows[-1]["payload"].get("depends_on", []))
        same = [r for r in dependency_states if r["payload"].get("file_id") == op.file_id]
        if same:
            known = agent_snapshot(self.journal.read_evidence(max(same, key=lambda r: r["seq"])["payload"]["observed_ref"]))
            if agent_semantic(known) != agent_semantic(baseline):
                raise SafetyError("DEPENDENCY_BASELINE_MISMATCH")
            baseline = known
        from .agent_profile import adjust_descendant_renames
        baseline = adjust_descendant_renames(baseline, dependency_states, self.journal)
        before = agent_snapshot(before)
        if self._agent_exact(before) != self._agent_exact(baseline):
            raise SafetyError("HANDOFF_BASELINE_DRIFT")
        expected = self._agent_checks(op, before, preflight, plan, approval)
        scope = {op.file_id} | set(before["parents"]) | set(op.target.get("parents", []))
        scope |= set(preflight.get("ancestor_ids", []))
        scope |= {m["id"] for m in (before.get("subtree") or {}).get("members", [])}
        scope |= set(getattr(op, "allowed_collision_ids", ()))
        for parent in preflight["parents"].values():
            scope |= set(parent["parents"])
        if scope & excluded:
            raise SafetyError("EXCLUDED")
        for value in scope:
            _id(value)
        if agent_semantic(before) == agent_semantic(expected):
            raise SafetyError("HANDOFF_NO_CHANGE_REQUIRED")
        return op, before, expected, copy.deepcopy(preflight), sorted(scope), plan_ref, authority["approval_ref"]

    def _reservation(self, scope, own_hash=None):
        latest = {}
        for row in self.journal.records():
            if row["kind"] in ACTION_STATES and row["operation_id"]:
                latest[row["operation_id"]] = row
        for row in latest.values():
            payload = row["payload"]
            if own_hash and payload.get("envelope_hash") == own_hash:
                continue
            if row["kind"] in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET", "FAILED_NOT_APPLIED", "SKIPPED"}:
                continue
            previous = set(payload.get("scope_ids", []))
            if payload.get("file_id"):
                previous.add(payload["file_id"])
            if payload.get("before_ref"):
                before = self.journal.read_evidence(payload["before_ref"])
                previous |= set(before["parents"])
                previous |= {member["id"] for member in (before.get("subtree") or {}).get("members", [])}
            if payload.get("intended_ref"):
                previous |= set(self.journal.read_evidence(payload["intended_ref"])["parents"])
            if previous & set(scope):
                raise SafetyError("HANDOFF_SCOPE_RESERVED")

    def _single_inflight(self, own_hash):
        latest = {}
        for row in self.journal.records():
            if row["kind"] in ACTION_STATES and row["operation_id"]:
                latest[row["operation_id"]] = row
        for row in latest.values():
            if (row["payload"].get("bridge") == _AGENT_BRIDGE
                    and row["payload"].get("envelope_hash") != own_hash
                    and row["kind"] in {"ATTEMPTED", "UNCERTAIN", "BLOCKED_CONFLICT"}):
                raise SafetyError("AGENT_OTHER_DISPATCH_UNRESOLVED")

    def prepare_action(self, plan, op_id, approval, before, preflight):
        with self.journal.locked():
            self._mode(dispatch=True)
            if (self.journal.root / "snapshot-manifest.json").exists():
                raise SafetyError("RESTORED_STORE_REQUIRES_RECONCILIATION")
            op, before, expected, check, scope, plan_ref, approval_ref = self._validate(
                plan, op_id, approval, before, preflight)
            previous = [r for r in self.journal.records() if r["operation_id"] == op_id and r["kind"] in ACTION_STATES]
            if previous:
                bridge_kind = _AGENT_BRIDGE if self.mode == "AGENT_ASSISTED" else _BRIDGE
                if previous[-1]["kind"] != "PREPARED" or previous[-1]["payload"].get("bridge") != bridge_kind:
                    raise SafetyError("HANDOFF_ALREADY_ATTEMPTED_OR_FINISHED")
                envelope, event = self._load(previous[-1]["payload"]["envelope_hash"])
                same_before = (self._agent_exact(self.journal.read_evidence(envelope["before_ref"])) == self._agent_exact(before)
                               if self.mode == "AGENT_ASSISTED" else self.journal.read_evidence(envelope["before_ref"]) == before)
                if (envelope["approval_ref"] != approval_ref or not same_before
                        or self.journal.read_evidence(envelope["preflight_ref"]) != check):
                    raise SafetyError("HANDOFF_PREPARED_CHANGED")
                self._reservation(scope, envelope["envelope_hash"])
                return envelope
            self._reservation(scope)
            args = {"fileId": op.file_id}
            if op.action == "RENAME":
                args["name"] = op.target["name"]
            else:
                add = sorted(set(op.target["parents"]) - set(before["parents"]))
                remove = sorted(set(before["parents"]) - set(op.target["parents"]))
                if add:
                    args["addParents"] = ",".join(add)
                if remove:
                    args["removeParents"] = ",".join(remove)
            prepared_at = datetime.now(timezone.utc)
            expires = min(prepared_at + timedelta(minutes=5), parsed(approval.expires_at))
            agent = self.mode == "AGENT_ASSISTED"
            envelope = {"schema_version": 1, "bridge": _AGENT_BRIDGE if agent else _BRIDGE, "mode": self.mode,
                        "envelope_id": str(uuid.uuid4()), "attempt_key": ("agent:" if agent else "fixture:") + str(uuid.uuid4()),
                        "run_id": plan.run_id, "batch_id": plan.batch_id,
                        "proposal_revision": plan.proposal_revision, "proposal_hash": plan.proposal_hash,
                        "plan_ref": plan_ref, "operation_id": op.operation_id, "file_id": op.file_id,
                        "action": op.action, "direction": op.direction,
                        "original_operation_id": op.original_operation_id,
                        "depends_on": list(op.depends_on), "proposal_refs": list(op.proposal_refs),
                        "approval_id": approval.approval_id, "approval_ref": approval_ref,
                        "actor": approval.actor, "before_ref": self.journal.evidence(before),
                        "intended_ref": self.journal.evidence(expected),
                        "preflight_ref": self.journal.evidence(check), "scope_ids": scope,
                        "prepared_at": prepared_at.isoformat(), "expires_at": expires.isoformat(),
                        "tool": AGENT_TOOL if agent else "fixture.drive.update_file", "args": args,
                        "dispatch_authorized": False, "live_execution_enabled": False}
            if agent:
                authority = self._agent_authority(plan, approval)
                envelope.update(controller_id=self.controller_id, authorization_ref=authority["authorization_ref"],
                                enable_ref=authority["enable_ref"], atomic_precondition=False,
                                limitations=sorted(AGENT_LIMITATIONS),
                                metadata_unavailable=before.get("metadata_unavailable", []),
                                permission_preservation_guaranteed=False)
            self.fault("after_handoff_evidence")
            envelope["envelope_hash"] = digest(envelope)
            envelope_ref = self.journal.evidence(envelope)
            payload = {k: v for k, v in envelope.items() if k not in {"args", "tool", "dispatch_authorized"}}
            payload.update(envelope_ref=envelope_ref, permission_ref=envelope["before_ref"],
                           reason_code="AGENT_HANDOFF_PREPARED" if agent else "TEST_ONLY_HANDOFF_PREPARED")
            self._emit("PREPARED", op.operation_id, payload)
            return envelope

    def mark_attempt(self, envelope_hash, observed_before, preflight):
        with self.journal.locked():
            self._mode(dispatch=True)
            envelope, prepared = self._load(envelope_hash)
            if envelope["mode"] != self.mode or (self.mode == "AGENT_ASSISTED" and envelope["controller_id"] != self.controller_id):
                raise SafetyError("HANDOFF_MODE_OR_CONTROLLER_MISMATCH")
            if any(r["kind"] == "ATTEMPTED" for r in self._events(envelope_hash)):
                raise SafetyError("HANDOFF_ATTEMPT_CONSUMED_RECONCILE")
            operation_states = [r for r in self.journal.records() if r["operation_id"] == envelope["operation_id"]
                                and r["kind"] in ACTION_STATES]
            if (not operation_states or operation_states[-1]["kind"] != "PREPARED"
                    or operation_states[-1]["payload"].get("envelope_hash") != envelope_hash):
                raise SafetyError("HANDOFF_OPERATION_STATE_CHANGED")
            if (self.journal.root / "snapshot-manifest.json").exists():
                raise SafetyError("RESTORED_STORE_REQUIRES_RECONCILIATION")
            if datetime.now(timezone.utc) >= parsed(envelope["expires_at"]):
                raise SafetyError("HANDOFF_PREPARED_EXPIRED")
            plan = _plan(self.journal.read_evidence(envelope["plan_ref"]))
            approval = Approval(**self.journal.read_evidence(envelope["approval_ref"]))
            op, before, expected, check, scope, _, _ = self._validate(
                plan, envelope["operation_id"], approval, observed_before, preflight)
            saved_check = self.journal.read_evidence(envelope["preflight_ref"])
            check_matches = (self._agent_safety(check) == self._agent_safety(saved_check)
                             if self.mode == "AGENT_ASSISTED" else check == saved_check)
            saved_before = self.journal.read_evidence(envelope["before_ref"])
            before_matches = (self._agent_exact(before) == self._agent_exact(saved_before)
                              if self.mode == "AGENT_ASSISTED" else before == saved_before)
            if (not before_matches
                    or not check_matches):
                raise SafetyError("HANDOFF_PRE_DISPATCH_DRIFT")
            self._reservation(scope, envelope_hash)
            if self.mode == "AGENT_ASSISTED":
                authority = self._agent_authority(plan, approval)
                if authority["enable_ref"] != envelope["enable_ref"] or authority["authorization_ref"] != envelope["authorization_ref"]:
                    raise SafetyError("AGENT_ENABLE_CHANGED_REPREPARE_REQUIRED")
                self._single_inflight(envelope_hash)
                self._agent_checks(op, before, check, plan, approval, minimum_time=envelope["prepared_at"])
            self.fault("after_final_handoff_check")
            attempted_payload = dict(prepared["payload"], reason_code=("AGENT_EXTERNAL_CALL_MAY_FOLLOW"
                                     if self.mode == "AGENT_ASSISTED" else "TEST_ONLY_EXTERNAL_CALL_MAY_FOLLOW"))
            if self.mode == "AGENT_ASSISTED":
                attempted_payload["final_preflight_ref"] = self.journal.evidence(check)
            self._emit("ATTEMPTED", op.operation_id, attempted_payload)
            self.fault("before_ticket_return")
            # This value is returned only once. A crash after the journal append
            # but before this return consumes it without proving any dispatch.
            return {"mode": self.mode, "envelope_hash": envelope_hash,
                    "attempt_key": envelope["attempt_key"], "tool": envelope["tool"],
                    "args": copy.deepcopy(envelope["args"]), "dispatch_ticket": str(uuid.uuid4()),
                    "live_execution_enabled": False, "agent_dispatch_authorized": self.mode == "AGENT_ASSISTED",
                    "atomic_precondition": self.mode == "TEST_ONLY"}

    def ingest_result(self, envelope_hash, attempt_key, raw, receipt_id=None):
        with self.journal.locked():
            self._mode()
            envelope, prepared = self._load(envelope_hash)
            if attempt_key != envelope["attempt_key"]:
                raise SafetyError("HANDOFF_ATTEMPT_KEY_MISMATCH")
            if not any(r["kind"] == "ATTEMPTED" for r in self._events(envelope_hash)):
                raise SafetyError("HANDOFF_NOT_ATTEMPTED")
            if not isinstance(raw, dict):
                raise SafetyError("HANDOFF_RESULT_OBJECT_REQUIRED")
            returned = raw.get("structuredContent", raw)
            if isinstance(returned, dict) and returned.get("id") is not None and returned["id"] != envelope["file_id"]:
                raise SafetyError("HANDOFF_RESULT_SOURCE_MISMATCH")
            raw_hash = digest(raw)
            try:
                receipt_id = str(uuid.UUID(receipt_id)) if receipt_id else str(uuid.uuid4())
            except (ValueError, TypeError, AttributeError) as exc:
                raise SafetyError("HANDOFF_RECEIPT_UUID_REQUIRED") from exc
            all_receipts = [r for r in self.journal.records() if r["kind"] == "HANDOFF_RESULT_RECEIVED"]
            for record in all_receipts:
                p = record["payload"]
                if p["receipt_id"] == receipt_id and (p["envelope_hash"] != envelope_hash or p["raw_hash"] != raw_hash):
                    raise SafetyError("HANDOFF_RECEIPT_UUID_CONFLICT")
            matches = [r for r in all_receipts if r["payload"]["envelope_hash"] == envelope_hash]
            if matches:
                prior = matches[0]["payload"]
                if prior["raw_hash"] != raw_hash:
                    raise SafetyError("HANDOFF_RESULT_CONFLICT")
                receipt_ref = prior["response_ref"]
                evidence = self.journal.read_evidence(receipt_ref)
                if digest(evidence["raw"]) != raw_hash:
                    raise SafetyError("HANDOFF_RESULT_HASH_MISMATCH")
            else:
                receipt_ref = self.journal.evidence({"mode": envelope["mode"], "envelope_hash": envelope_hash,
                              "attempt_key": attempt_key, "receipt_id": receipt_id, "raw_hash": raw_hash,
                              "raw": copy.deepcopy(raw)})
                self.fault("after_result_evidence")
                self._emit("HANDOFF_RESULT_RECEIVED", envelope["operation_id"],
                           {"bridge": envelope["bridge"], "envelope_hash": envelope_hash, "attempt_key": attempt_key,
                            "receipt_id": receipt_id, "raw_hash": raw_hash, "response_ref": receipt_ref,
                            "mode": envelope["mode"]})
            latest = self._latest(envelope_hash)
            if latest["kind"] == "ATTEMPTED":
                self._emit("UNCERTAIN", envelope["operation_id"], dict(prepared["payload"],
                           response_ref=receipt_ref, reason_code="RAW_RESULT_REQUIRES_INDEPENDENT_READBACK"))
            return receipt_ref

    def reconcile_action(self, envelope_hash, observed, postflight, terminal_evidence):
        with self.journal.locked():
            self._mode()
            envelope, prepared = self._load(envelope_hash)
            if not any(r["kind"] == "ATTEMPTED" for r in self._events(envelope_hash)):
                raise SafetyError("HANDOFF_NOT_ATTEMPTED")
            latest = self._latest(envelope_hash)
            if latest["kind"] in _TERMINAL:
                return latest
            if envelope["mode"] == "AGENT_ASSISTED":
                return self._reconcile_agent(envelope, prepared, observed, postflight, terminal_evidence)
            terminal = (self.journal.read_evidence(terminal_evidence)
                        if isinstance(terminal_evidence, str) else terminal_evidence)
            if (not isinstance(terminal, dict) or terminal.get("mode") != "TEST_ONLY"
                    or terminal.get("kind") != "FIXTURE_TERMINAL"
                    or terminal.get("attempt_key") != envelope["attempt_key"]
                    or terminal.get("envelope_hash") != envelope_hash
                    or type(terminal.get("terminal")) is not bool):
                raise SafetyError("HANDOFF_TERMINAL_PROOF_MISMATCH")
            payload = dict(prepared["payload"])
            payload["terminal_ref"] = self.journal.evidence(terminal)
            payload["observed_ref"] = self.journal.evidence(observed)
            payload["postflight_ref"] = self.journal.evidence(postflight)
            self.fault("after_reconciliation_evidence")
            before = self.journal.read_evidence(envelope["before_ref"])
            intended = self.journal.read_evidence(envelope["intended_ref"])
            check = self.journal.read_evidence(envelope["preflight_ref"])
            if terminal["terminal"] is not True:
                kind, reason = "UNCERTAIN", "REQUEST_NOT_PROVEN_TERMINAL"
            else:
                try:
                    observed = snapshot(observed)
                    if observed["id"] != envelope["file_id"] or postflight != check:
                        kind, reason = "BLOCKED_CONFLICT", "HANDOFF_POSTFLIGHT_DRIFT"
                    elif semantic(observed) == semantic(intended):
                        kind, reason = "RECONCILED_AT_TARGET", "OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN"
                    elif observed == before:
                        kind, reason = "FAILED_NOT_APPLIED", "TERMINAL_AND_PREIMAGE_VERIFIED"
                    else:
                        kind, reason = "BLOCKED_CONFLICT", "HANDOFF_RECONCILIATION_DRIFT"
                except (SafetyError, TypeError, KeyError, AttributeError):
                    kind, reason = "BLOCKED_CONFLICT", "HANDOFF_METADATA_INCOMPLETE"
            payload.update(reason_code=reason, reconciled_at=now())
            return self._emit(kind, envelope["operation_id"], payload)

    def _reconcile_agent(self, envelope, prepared, observed, postflight, terminal_evidence):
        from .agent_profile import agent_snapshot
        terminal = (self.journal.read_evidence(terminal_evidence)
                    if isinstance(terminal_evidence, str) else terminal_evidence)
        if (not isinstance(terminal, dict) or terminal.get("mode") != "AGENT_ASSISTED"
                or terminal.get("attempt_key") != envelope["attempt_key"]
                or terminal.get("envelope_hash") != envelope["envelope_hash"]
                or type(terminal.get("terminal")) is not bool):
            raise SafetyError("HANDOFF_TERMINAL_PROOF_MISMATCH")
        attempted = [r for r in self._events(envelope["envelope_hash"]) if r["kind"] == "ATTEMPTED"][0]
        minimum_time = attempted["at"]
        if terminal["terminal"]:
            if terminal.get("kind") != "AGENT_TOOL_COMPLETED":
                raise SafetyError("AGENT_TERMINAL_COMPLETION_REQUIRED")
            receipts = [r for r in self._events(envelope["envelope_hash"]) if r["kind"] == "HANDOFF_RESULT_RECEIVED"
                        and r["payload"]["response_ref"] == terminal.get("response_ref")]
            if len(receipts) != 1:
                raise SafetyError("AGENT_TERMINAL_RECEIPT_REQUIRED")
            receipt = self.journal.read_evidence(terminal["response_ref"])
            completion = self._provenance(terminal.get("provenance_ref"), "TRUSTED_TOOL_COMPLETION")
            raw = receipt["raw"]
            returned = raw.get("structuredContent", {})
            if (raw.get("isError") is True or not isinstance(returned, dict) or returned.get("id") != envelope["file_id"]
                    or completion.get("outcome") != "completed_success" or not completion.get("tool_call_id")
                    or completion.get("tool") != AGENT_TOOL or completion.get("args_hash") != digest(envelope["args"])
                    or completion.get("raw_response_hash") != digest(raw)
                    or not parsed(attempted["at"]) <= parsed(completion["completed_at"]) <= datetime.now(timezone.utc)):
                raise SafetyError("AGENT_TERMINAL_COMPLETION_UNPROVEN")
            minimum_time = completion["completed_at"]
        elif terminal.get("kind") != "AGENT_UNRESOLVED":
            raise SafetyError("AGENT_UNRESOLVED_EVIDENCE_REQUIRED")
        payload = dict(prepared["payload"])
        payload.update(terminal_ref=self.journal.evidence(terminal), observed_ref=self.journal.evidence(observed),
                       postflight_ref=self.journal.evidence(postflight))
        self.fault("after_reconciliation_evidence")
        if not terminal["terminal"]:
            return self._emit("UNCERTAIN", envelope["operation_id"], payload |
                              {"reason_code": "REQUEST_NOT_PROVEN_TERMINAL", "reconciled_at": now()})
        before = self.journal.read_evidence(envelope["before_ref"])
        intended = self.journal.read_evidence(envelope["intended_ref"])
        baseline_check = self.journal.read_evidence(attempted["payload"].get("final_preflight_ref", envelope["preflight_ref"]))
        plan = _plan(self.journal.read_evidence(envelope["plan_ref"]))
        op = next(op for op in plan.operations if op.operation_id == envelope["operation_id"])
        approval = Approval(**self.journal.read_evidence(envelope["approval_ref"]))
        try:
            observed = agent_snapshot(observed)
            self._agent_checks(op, before, postflight, plan, approval, source=observed,
                               minimum_time=minimum_time, controller_id=envelope["controller_id"])
            core = lambda value: {k: value[k] for k in ("id", "name", "mime_type", "parents")}
            members = lambda value: {m["id"] for m in (value.get("subtree") or {}).get("members", [])}
            at_target = core(observed) == core(intended) and members(observed) == members(intended)
            at_before = core(observed) == core(before) and members(observed) == members(before)
            if not at_target and not at_before:
                raise SafetyError("AGENT_OBSERVED_METADATA_DRIFT")
            stable = {"owners", "drive_id", "shortcut_details", "md5_checksum", "size"}
            if op.action == "RENAME" or at_before:
                stable |= {"permissions", "access", "capabilities"}
            baseline = intended if at_target else before
            if any(baseline.get(key) is not None and observed.get(key) != baseline[key] for key in stable):
                raise SafetyError("AGENT_SOURCE_KNOWN_FIELD_DRIFT")
            if op.action == "MOVE" and at_target:
                direct = [grant for grant in before.get("permissions", []) or []
                          if any(detail.get("inherited") is False
                                 for detail in grant.get("permissionDetails", []))]
                if any(grant not in (observed.get("permissions") or []) for grant in direct):
                    raise SafetyError("AGENT_DIRECT_PERMISSION_DRIFT")
            # A move predictably changes the full membership of its two direct
            # parent folders. Preserve every unrelated member by stable ID.
            moving = {op.file_id} | members(before)
            for parent_id, prior in baseline_check["parents"].items():
                after_parent = postflight["parents"][parent_id]
                if core(prior) != core(after_parent):
                    raise SafetyError("AGENT_PARENT_IDENTITY_DRIFT")
                stable_parent = {"permissions", "owners", "access", "capabilities", "drive_id"}
                if any(prior.get(key) is not None and after_parent.get(key) != prior[key] for key in stable_parent):
                    raise SafetyError("AGENT_PARENT_KNOWN_FIELD_DRIFT")
                expected_members = members(prior)
                if at_target and op.action == "MOVE":
                    if parent_id in set(before["parents"]) - set(intended["parents"]):
                        expected_members -= moving
                    if parent_id in set(intended["parents"]) - set(before["parents"]):
                        expected_members |= moving
                if members(after_parent) != expected_members:
                    raise SafetyError("AGENT_PARENT_MEMBERSHIP_DRIFT")
            kind = "RECONCILED_AT_TARGET" if at_target else "FAILED_NOT_APPLIED"
            reason = ("OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN" if at_target
                      else "TERMINAL_PREIMAGE_OBSERVED_ATTRIBUTION_UNCERTAIN")
        except (SafetyError, KeyError, TypeError, AttributeError, ValueError):
            kind, reason = "BLOCKED_CONFLICT", "AGENT_READBACK_OR_MEMBERSHIP_CONFLICT"
        payload.update(reason_code=reason, reconciled_at=now())
        return self._emit(kind, envelope["operation_id"], payload)
