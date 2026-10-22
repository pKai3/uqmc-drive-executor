"""Strict, stable-ID plans. No credentials and no Drive writes live here."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

# Test sentinel only. Real permanently withdrawn IDs live in ignored private policy.
WITHDRAWN_ID = "fixture-withdrawn"
FOLDER = "application/vnd.google-apps.folder"
NATIVE = {
    "application/vnd.google-apps.document": ".gdoc",
    "application/vnd.google-apps.spreadsheet": ".gsheet",
    "application/vnd.google-apps.presentation": ".gslides",
    "application/vnd.google-apps.form": ".gform",
}
MIME_EXTENSIONS = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
                   "application/pdf": {".pdf"}, "image/png": {".png"},
                   "image/jpeg": {".jpg", ".jpeg"}, "image/gif": {".gif"},
                   "image/webp": {".webp"}, "image/heic": {".heic"},
                   "image/heif": {".heif"},
                   "image/tiff": {".tif", ".tiff"}}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def uploaded_extension(title):
    """A dotted filename stem is not an extension; retain known compound archives."""
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if title.lower().endswith(compound):
            return title[-len(compound):]
    return PurePosixPath(title).suffix


def validate_rename_name(op, before):
    title, old = op.target["name"], before["name"]
    if not isinstance(title, str) or not title or "\x00" in title:
        raise SafetyError("INVALID_TITLE")
    # Registration checks a rollback's exact target against the original
    # verified preimage. Historical titles need not satisfy new naming rules.
    if op.direction == "ROLLBACK":
        return
    if before["mime_type"] in NATIVE:
        if any(title.lower().endswith(ext) and not old.lower().endswith(ext) for ext in NATIVE.values()):
            raise SafetyError("SYNTHETIC_NATIVE_EXTENSION")
    elif before["mime_type"] != FOLDER:
        suffix = uploaded_extension(old)
        # Casing is part of the approved title, not a change of file format.
        # Keep exact names in evidence/rollback while comparing extensions
        # case-insensitively (including known compound archive suffixes).
        if suffix and not title.casefold().endswith(suffix.casefold()):
            target_suffix = uploaded_extension(title)
            correction = op.extension_correction
            allowed = MIME_EXTENSIONS.get(before["mime_type"], set())
            required = {"source_suffix", "target_suffix", "mime_type", "evidence_ref"}
            if (not isinstance(correction, dict) or set(correction) != required
                    or correction.get("source_suffix") != suffix
                    or correction.get("target_suffix") != target_suffix
                    or correction.get("mime_type") != before["mime_type"]
                    or not correction.get("evidence_ref")
                    or suffix.lower() in allowed or target_suffix.lower() not in allowed):
                raise SafetyError("UPLOADED_EXTENSION_CHANGED")


def validate_extension_evidence(journal, op):
    if not op.extension_correction:
        return
    if op.action != "RENAME" or op.direction != "FORWARD":
        raise SafetyError("EXTENSION_CORRECTION_SCOPE")
    proof = journal.read_evidence(op.extension_correction["evidence_ref"])
    expected = {"kind": "VERIFIED_EXTENSION_CORRECTION", "source_id": op.file_id,
                "source_exact_name": op.before["name"], "target_exact_name": op.target["name"],
                "mime_type": op.before["mime_type"]}
    if any(proof.get(key) != value for key, value in expected.items()) or not proof.get("provenance_ref"):
        raise SafetyError("EXTENSION_CORRECTION_EVIDENCE_MISMATCH")
    journal.read_evidence(proof["provenance_ref"])


def validate_rollback_links(journal, plan):
    """Bind inverse candidates to frozen, verified originals and exact images.

    This is a local lineage check, not a live read or authority grant. The
    handoff must still compare fresh metadata against the frozen inverse image.
    """
    inverse_ops = [op for op in plan.ordered() if op.direction == "ROLLBACK"]
    if not inverse_ops:
        return
    if len(inverse_ops) != len(plan.operations):
        raise SafetyError("MIXED_ROLLBACK_PLAN")
    selected = {op.original_operation_id for op in inverse_ops}
    if len(selected) != len(inverse_ops):
        raise SafetyError("DUPLICATE_ROLLBACK_ORIGINAL")
    records = journal.records()
    verified = {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"}
    action_states = verified | {"PREPARED", "ATTEMPTED", "UNCERTAIN", "FAILED_NOT_APPLIED", "BLOCKED_CONFLICT", "SKIPPED"}
    latest = {}
    for event in records:
        if event["operation_id"] and event["kind"] in action_states:
            latest[event["operation_id"]] = event
    own_ids = {op.operation_id for op in inverse_ops}
    if any(event["operation_id"] not in own_ids and (event["kind"] in {"ATTEMPTED", "UNCERTAIN"}
           or (event["kind"] == "PREPARED" and event["payload"].get("bridge"))) for event in latest.values()):
        raise SafetyError("RECONCILE_BEFORE_ROLLBACK")
    originals, images, compare, original_plans = {}, {}, {}, {}
    for op in inverse_ops:
        source = [r for r in records if r["operation_id"] == op.original_operation_id
                  and r["kind"] in verified and r["payload"].get("direction") == "FORWARD"]
        if not source:
            raise SafetyError("ROLLBACK_ORIGINAL_NOT_VERIFIED")
        event = source[-1]
        payload = event["payload"]
        if latest[op.original_operation_id]["kind"] not in verified:
            raise SafetyError("ROLLBACK_STATE_REQUIRES_RECONCILIATION")
        prior_inverses = [r for r in records if r["kind"] in verified
                          and r["payload"].get("direction") == "ROLLBACK"
                          and r["payload"].get("original_operation_id") == op.original_operation_id]
        if any(r["operation_id"] != op.operation_id or r["payload"].get("proposal_hash") != plan.proposal_hash for r in prior_inverses):
            raise SafetyError("ROLLBACK_ALREADY_REVERTED")
        drafts = [r for r in records if r["kind"] == "DRAFTED"
                  and r["payload"].get("run_id") == payload.get("run_id")
                  and r["payload"].get("proposal_hash") == payload.get("proposal_hash")]
        if not drafts:
            raise SafetyError("ROLLBACK_ORIGINAL_NOT_FROZEN")
        body = journal.read_evidence(drafts[-1]["payload"]["plan_ref"])
        if digest(body) != payload.get("proposal_hash"):
            raise SafetyError("ROLLBACK_FROZEN_PLAN_MISMATCH")
        forward = Plan(body["run_id"], body["batch_id"], body["proposal_revision"],
                       tuple(Operation(**item) for item in body["operations"]), tuple(body.get("excluded_ids", ())))
        matches = [item for item in forward.operations if item.operation_id == op.original_operation_id]
        if len(matches) != 1 or matches[0].direction != "FORWARD":
            raise SafetyError("ROLLBACK_ORIGINAL_NOT_IN_FROZEN_PLAN")
        original = matches[0]
        if drafts[-1]["payload"].get("profile") == "AGENT_ASSISTED":
            from .agent_profile import agent_semantic, agent_postimage_matches
            comparator = agent_semantic
            postimage_matches = lambda intended, observed: agent_postimage_matches(original, intended, observed)
        else:
            comparator = semantic
            postimage_matches = lambda intended, observed: semantic(intended) == semantic(observed)
        if any(not payload.get(key) for key in ("before_ref", "intended_ref", "observed_ref")):
            raise SafetyError("ROLLBACK_EXACT_IMAGES_REQUIRED")
        before, intended, after = [journal.read_evidence(payload[key]) for key in ("before_ref", "intended_ref", "observed_ref")]
        field = "name" if op.action == "RENAME" else "parents"
        if (payload.get("file_id") != op.file_id or payload.get("action") != op.action
                or original.file_id != op.file_id or original.action != op.action
                or op.target != {field: before[field]}):
            raise SafetyError("ROLLBACK_NOT_EXACT_PREIMAGE")
        if comparator(before) != comparator(original.before):
            raise SafetyError("ROLLBACK_PREIMAGE_MISMATCH")
        if (any(intended[key] != original.intended(before)[key] for key in ("id", "name", "mime_type", "parents"))
                or not postimage_matches(intended, after)):
            raise SafetyError("ROLLBACK_EXACT_POSTIMAGE_REQUIRED")
        originals[op.original_operation_id] = original
        images[op.original_operation_id] = (before, after)
        compare[op.original_operation_id] = comparator
        original_plans[forward.proposal_hash] = forward
    inverse_closure = {}
    by_original = {op.original_operation_id: op for op in inverse_ops}
    reverted = {r["payload"].get("original_operation_id") for r in records
                if r["kind"] in verified and r["payload"].get("direction") == "ROLLBACK"}
    for op in inverse_ops:
        inverse_closure[op.operation_id] = set(op.depends_on)
        for dep in op.depends_on:
            inverse_closure[op.operation_id] |= inverse_closure[dep]
    for forward in original_plans.values():
        closure = {}
        for original in forward.ordered():
            closure[original.operation_id] = set(original.depends_on)
            for dep in original.depends_on:
                closure[original.operation_id] |= closure[dep]
            active = original.operation_id in latest and latest[original.operation_id]["kind"] in verified and original.operation_id not in reverted
            if active and original.operation_id not in selected and closure[original.operation_id] & selected:
                raise SafetyError("ROLLBACK_HAS_ACTIVE_DEPENDENTS")
            if original.operation_id in selected:
                for predecessor in closure[original.operation_id] & selected:
                    if by_original[original.operation_id].operation_id not in inverse_closure[by_original[predecessor].operation_id]:
                        raise SafetyError("ROLLBACK_DEPENDENCY_ORDER")
    simulated, child_changes = {}, []
    for op in inverse_ops:
        before, after = images[op.original_operation_id]
        baseline = copy.deepcopy(simulated.get(op.file_id, after))
        for child_id, previous_name, restored_name in child_changes:
            for member in (baseline.get("subtree") or {}).get("members", []):
                if member["id"] == child_id and "name" in member:
                    if member["name"] == previous_name:
                        member["name"] = restored_name
                    elif member["name"] != restored_name:
                        raise SafetyError("ROLLBACK_DESCENDANT_CHAIN_DRIFT")
        comparator = compare[op.original_operation_id]
        if comparator(baseline) != comparator(after) or comparator(op.before) != comparator(baseline):
            raise SafetyError("ROLLBACK_POSTIMAGE_BASELINE_MISMATCH")
        simulated[op.file_id] = copy.deepcopy(before)
        if op.action == "RENAME":
            child_changes.append((op.file_id, after["name"], before["name"]))


class SafetyError(RuntimeError):
    pass


def snapshot(value):
    """Reject missing data; omitted permissions/capabilities never become empty ones."""
    out = copy.deepcopy(value)
    required = {"id", "name", "mime_type", "parents", "version", "modified_time", "trashed",
                "permissions", "owners", "drive_id", "capabilities", "access", "subtree"}
    if not required <= out.keys():
        raise SafetyError("INCOMPLETE_METADATA")
    if not isinstance(out["name"], str) or not isinstance(out["parents"], list):
        raise SafetyError("INVALID_METADATA")
    if len(out["parents"]) != len(set(out["parents"])) or any(not isinstance(x, str) or not x for x in out["parents"]):
        raise SafetyError("INVALID_PARENTS")
    out["parents"] = sorted(out["parents"])
    if not out["version"] or not out["modified_time"] or out["trashed"] is not False:
        raise SafetyError("TRASHED_OR_UNVERSIONED")
    if not isinstance(out["permissions"], list) or not isinstance(out["owners"], list):
        raise SafetyError("INCOMPLETE_PERMISSIONS")
    if not out["access"].get("complete"):
        raise SafetyError("INCOMPLETE_EFFECTIVE_ACCESS")
    if out["mime_type"] == FOLDER and not out["subtree"].get("complete"):
        raise SafetyError("INCOMPLETE_DESCENDANTS")
    return out


def semantic(value):
    """Compare all safety metadata, allowing only version/time changes from known actions."""
    return {k: v for k, v in snapshot(value).items() if k not in ("version", "modified_time")}


@dataclass(frozen=True)
class Operation:
    operation_id: str
    file_id: str
    action: str
    before: dict
    target: dict
    depends_on: tuple[str, ...] = ()
    proposal_refs: tuple[str, ...] = ()
    direction: str = "FORWARD"
    original_operation_id: str | None = None
    allowed_collision_ids: tuple[str, ...] = ()
    extension_correction: dict = field(default_factory=dict)

    def intended(self, before=None):
        result = copy.deepcopy(self.before if before is None else before)
        result.update(copy.deepcopy(self.target))
        if "parents" in result:
            result["parents"] = sorted(result["parents"])
        return result

    def validate(self):
        if not self.operation_id or not self.file_id or self.file_id == WITHDRAWN_ID:
            raise SafetyError("EXCLUDED")
        state = snapshot(self.before)
        if state["id"] != self.file_id:
            raise SafetyError("ID_MISMATCH")
        if self.action not in ("MOVE", "RENAME"):
            raise SafetyError("UNSUPPORTED_ACTION")
        if (len(set(self.allowed_collision_ids)) != len(self.allowed_collision_ids)
                or any(not isinstance(item, str) or not item for item in self.allowed_collision_ids)
                or self.file_id in self.allowed_collision_ids):
            raise SafetyError("INVALID_COLLISION_SCOPE")
        if self.direction not in ("FORWARD", "ROLLBACK"):
            raise SafetyError("INVALID_DIRECTION")
        if (self.direction == "ROLLBACK") != bool(self.original_operation_id):
            raise SafetyError("ROLLBACK_LINK_REQUIRED")
        key = "parents" if self.action == "MOVE" else "name"
        if set(self.target) != {key}:
            raise SafetyError("FIELD_SCOPE")
        if key == "parents":
            if not isinstance(self.target[key], list) or not self.target[key] or len(set(self.target[key])) != len(self.target[key]):
                raise SafetyError("INVALID_PARENTS")
            if self.file_id in self.target[key] or WITHDRAWN_ID in self.target[key]:
                raise SafetyError("INVALID_DESTINATION")
        else:
            validate_rename_name(self, state)
        snapshot(self.intended())


@dataclass(frozen=True)
class Plan:
    run_id: str
    batch_id: str
    proposal_revision: str
    operations: tuple[Operation, ...]
    excluded_ids: tuple[str, ...] = ()

    def body(self):
        return asdict(self)

    @property
    def proposal_hash(self):
        return digest(self.body())

    def ordered(self):
        lookup = {op.operation_id: op for op in self.operations}
        if len(lookup) != len(self.operations):
            raise SafetyError("DUPLICATE_OPERATION")
        result, pending = [], list(self.operations)
        while pending:
            ready = [op for op in pending if set(op.depends_on) <= {x.operation_id for x in result}]
            if not ready:
                raise SafetyError("DEPENDENCY_CYCLE_OR_MISSING")
            for op in ready:
                result.append(op)
                pending.remove(op)
        return result

    def validate(self):
        if not self.run_id or not self.batch_id or not self.proposal_revision:
            raise SafetyError("MISSING_PLAN_IDENTITY")
        excluded = set(self.excluded_ids) | {WITHDRAWN_ID}
        seen = {}
        ancestors = {}
        for op in self.ordered():
            op.validate()
            if op.file_id in excluded or set(op.before["parents"]) & excluded or set(op.target.get("parents", [])) & excluded:
                raise SafetyError("EXCLUDED")
            if set(op.allowed_collision_ids) & excluded:
                raise SafetyError("EXCLUDED_COLLISION_SCOPE")
            members = {x["id"] for x in op.before["subtree"].get("members", [])}
            if members & excluded:
                raise SafetyError("EXCLUDED_DESCENDANT")
            closure = set(op.depends_on)
            for dep in op.depends_on:
                closure |= ancestors[dep]
            ancestors[op.operation_id] = closure
            if op.file_id in seen and seen[op.file_id] not in closure:
                raise SafetyError("UNORDERED_SAME_FILE")
            seen[op.file_id] = op.operation_id
        # Overlapping folder/descendant actions need an explicit dependency chain.
        for i, left in enumerate(self.operations):
            for right in self.operations[i + 1:]:
                lm = {m["id"] for m in left.before["subtree"].get("members", [])}
                rm = {m["id"] for m in right.before["subtree"].get("members", [])}
                if left.file_id in rm or right.file_id in lm:
                    if left.operation_id not in ancestors[right.operation_id] and right.operation_id not in ancestors[left.operation_id]:
                        raise SafetyError("UNORDERED_FOLDER_OVERLAP")


@dataclass(frozen=True)
class Approval:
    approval_id: str
    run_id: str
    batch_id: str
    proposal_revision: str
    proposal_hash: str
    operation_ids: tuple[str, ...]
    actor: str
    approved_at: str
    expires_at: str
    mode: str = "TEST_ONLY"
    access_change_hashes: dict = field(default_factory=dict)

    def body(self):
        return asdict(self)
