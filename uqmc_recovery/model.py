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
}
MIME_EXTENSIONS = {"application/pdf": {".pdf"}, "image/png": {".png"},
                   "image/jpeg": {".jpg", ".jpeg"}, "image/gif": {".gif"},
                   "image/webp": {".webp"}, "image/heic": {".heic"},
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
        if suffix and not title.endswith(suffix):
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
    records = journal.records()
    for op in plan.operations:
        if op.direction != "ROLLBACK":
            continue
        source = [r for r in records if r["operation_id"] == op.original_operation_id
                  and r["kind"] in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"}
                  and r["payload"].get("direction") == "FORWARD"]
        if not source:
            raise SafetyError("ROLLBACK_ORIGINAL_NOT_VERIFIED")
        event = source[-1]
        before = journal.read_evidence(event["payload"]["before_ref"])
        key = "name" if op.action == "RENAME" else "parents"
        if (event["payload"]["file_id"] != op.file_id or event["payload"]["action"] != op.action
                or op.target != {key: before[key]}):
            raise SafetyError("ROLLBACK_NOT_EXACT_PREIMAGE")


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
