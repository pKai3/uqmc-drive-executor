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


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


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
            title = self.target[key]
            if not isinstance(title, str) or not title or "\x00" in title:
                raise SafetyError("INVALID_TITLE")
            old = state["name"]
            if state["mime_type"] in NATIVE:
                # An existing literal suffix may be preserved; display suffixes cannot be added.
                if any(title.lower().endswith(ext) and not old.lower().endswith(ext) for ext in NATIVE.values()):
                    raise SafetyError("SYNTHETIC_NATIVE_EXTENSION")
            elif state["mime_type"] != FOLDER:
                suffixes = "".join(PurePosixPath(old).suffixes)
                if suffixes and not title.endswith(suffixes):
                    raise SafetyError("UPLOADED_EXTENSION_CHANGED")
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
