"""Agent-assisted, read-only connector request/receipt bridge.

This module never invokes tools, network calls, credentials, or mutations. Its
output is a frozen list of metadata reads that an already-authorized agent may
perform separately. Imported receipts are private observations, not approval,
proof of effective access, proof of a mutation, or live-execution readiness.
"""
from __future__ import annotations

import copy
import re
import uuid

from .adapters import assess_connector_metadata
from .model import SafetyError, WITHDRAWN_ID, digest, snapshot


READ_TOOL = "mcp__codex_apps__google_drive_get_file_metadata"
METADATA_FIELDS = (
    "id,name,mimeType,parents,version,modifiedTime,trashed,owners,driveId,"
    "capabilities,permissions,permissionIds,inheritedPermissionsDisabled,"
    "shared,hasAugmentedPermissions,shortcutDetails"
)
_ID = re.compile(r"[A-Za-z0-9_-]+")


def _file_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value == "root":
        raise SafetyError("READ_STABLE_ID_REQUIRED")
    return value


def _tombstones(records):
    denied, refs = {WITHDRAWN_ID}, set()
    for record in records:
        payload = record["payload"]
        if record["kind"] == "EXCLUDED":
            denied.add(_file_id(payload["file_id"]))
        if record["kind"] == "PROPOSAL_EXCLUDED":
            refs.add(payload["proposal_ref"])
    return denied, refs


def _read_targets(plan_body, denied_ids, denied_refs):
    """Resolve only IDs already present in the frozen proposal; never by title."""
    if not isinstance(plan_body, dict):
        raise SafetyError("READ_INVALID_PLAN")
    for key in ("run_id", "batch_id", "proposal_revision"):
        if not isinstance(plan_body.get(key), str) or not plan_body[key]:
            raise SafetyError("READ_PLAN_IDENTITY_REQUIRED")
    operations = plan_body.get("operations")
    if not isinstance(operations, (list, tuple)) or not operations:
        raise SafetyError("READ_OPERATIONS_REQUIRED")
    denied = set(denied_ids)
    excluded = plan_body.get("excluded_ids", [])
    if not isinstance(excluded, (list, tuple)):
        raise SafetyError("READ_INVALID_EXCLUSIONS")
    denied.update(_file_id(value) for value in excluded)
    targets = {}
    seen_operations = set()
    for operation in operations:
        if not isinstance(operation, dict):
            raise SafetyError("READ_INVALID_OPERATION")
        if (operation.get("excluded") is True or operation.get("withdrawn") is True
                or operation.get("status") in ("EXCLUDED", "WITHDRAWN")):
            raise SafetyError("EXCLUDED")
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id or operation_id in seen_operations:
            raise SafetyError("READ_OPERATION_ID_REQUIRED")
        seen_operations.add(operation_id)
        file_id = _file_id(operation.get("file_id"))
        if operation.get("action") not in ("MOVE", "RENAME"):
            raise SafetyError("READ_UNSUPPORTED_PROPOSAL_ACTION")
        before, target = operation.get("before"), operation.get("target")
        if not isinstance(before, dict) or not isinstance(target, dict):
            raise SafetyError("READ_INVALID_OPERATION")
        if before.get("id", file_id) != file_id:
            raise SafetyError("READ_SOURCE_ID_MISMATCH")
        before_parents = before.get("parents")
        target_parents = target.get("parents", [])
        if not isinstance(before_parents, (list, tuple)) or not isinstance(target_parents, (list, tuple)):
            raise SafetyError("READ_PARENT_IDS_REQUIRED")
        proposal_refs = operation.get("proposal_refs", [])
        if not isinstance(proposal_refs, (list, tuple)):
            raise SafetyError("READ_INVALID_PROPOSAL_REFS")
        if set(proposal_refs) & denied_refs:
            raise SafetyError("PROPOSAL_EXCLUDED")
        ids = [(file_id, "source")]
        ids += [(_file_id(value), "before_parent") for value in before_parents]
        ids += [(_file_id(value), "target_parent") for value in target_parents]
        if {value for value, _ in ids} & denied:
            raise SafetyError("EXCLUDED")
        # A folder scope containing a withdrawn descendant remains excluded even
        # though this read bundle does not enumerate or fetch descendants.
        subtree = before.get("subtree", {})
        if not isinstance(subtree, dict) or not isinstance(subtree.get("members", []), list):
            raise SafetyError("READ_INVALID_SUBTREE")
        for member in subtree.get("members", []):
            if not isinstance(member, dict) or _file_id(member.get("id")) in denied:
                raise SafetyError("EXCLUDED_DESCENDANT")
        for value, role in ids:
            targets.setdefault(value, set()).add(role)
    return {file_id: sorted(roles) for file_id, roles in sorted(targets.items())}


def prepare_reads(journal, plan_body: dict, denied_ids: set) -> dict:
    """Freeze a read-only request without dispatching any connector operation."""
    with journal.locked():
        records = journal.records()
        denied, denied_refs = _tombstones(records)
        denied.update(_file_id(value) for value in denied_ids)
        body = copy.deepcopy(plan_body)
        targets = _read_targets(body, denied, denied_refs)
        plan_hash = digest(body)
        plan_ref = journal.evidence(body)
        bundle = {
            "schema_version": 1,
            "request_id": str(uuid.uuid4()),
            "run_id": body["run_id"],
            "batch_id": body["batch_id"],
            "proposal_revision": body["proposal_revision"],
            "plan_hash": plan_hash,
            "plan_ref": plan_ref,
            "expected_file_ids": list(targets),
            "denied_ids": sorted(denied),
            "requests": [{"file_id": file_id, "roles": roles, "tool": READ_TOOL,
                          "args": {"fileId": file_id, "fields": METADATA_FIELDS,
                                   "supportsAllDrives": True}}
                         for file_id, roles in targets.items()],
            "read_only": True,
            "execution_ready": False,
        }
        saved = journal.append("READ_PREPARED", payload=bundle)
        return saved["payload"]


def _prepared(journal, request_id, *, enforce_exclusions=True):
    if not isinstance(request_id, str):
        raise SafetyError("READ_REQUEST_UNKNOWN")
    records = journal.records()
    candidates = [record["payload"] for record in records
                  if record["kind"] == "READ_PREPARED" and record["payload"].get("request_id") == request_id]
    if len(candidates) != 1:
        raise SafetyError("READ_REQUEST_UNKNOWN_OR_AMBIGUOUS")
    prepared = candidates[0]
    body = journal.read_evidence(prepared["plan_ref"])
    if digest(body) != prepared["plan_hash"]:
        raise SafetyError("READ_PLAN_HASH_MISMATCH")
    denied, denied_refs = _tombstones(records)
    denied.update(prepared["denied_ids"])
    targets = _read_targets(body, denied if enforce_exclusions else set(prepared["denied_ids"]),
                            denied_refs if enforce_exclusions else set())
    if list(targets) != prepared["expected_file_ids"]:
        raise SafetyError("READ_EXPECTED_IDS_MISMATCH")
    return prepared, records


def _metadata(raw):
    if not isinstance(raw, dict) or raw.get("isError") is True:
        raise SafetyError("READ_RECEIPT_NOT_METADATA")
    metadata = raw.get("structuredContent", raw)
    if not isinstance(metadata, dict):
        raise SafetyError("READ_RECEIPT_NOT_METADATA")
    return metadata


def ingest_receipt(journal, request_id: str, file_id: str, raw: dict,
                   receipt_id: str | None = None) -> str:
    """Import one exact response. Identical retries reuse the original evidence.

    A different response for the same request/file needs a fresh read request;
    history is never replaced. UUID receipt IDs cannot be reused for other data.
    """
    with journal.locked():
        prepared, records = _prepared(journal, request_id)
        file_id = _file_id(file_id)
        if file_id not in prepared["expected_file_ids"]:
            raise SafetyError("READ_FILE_NOT_REQUESTED")
        metadata = _metadata(raw)
        if metadata.get("id") != file_id:
            raise SafetyError("READ_RECEIPT_ID_MISMATCH")
        raw_hash = digest(raw)
        if receipt_id is not None:
            try:
                receipt_id = str(uuid.UUID(receipt_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise SafetyError("READ_RECEIPT_UUID_REQUIRED") from exc
        receipts = [record["payload"] for record in records if record["kind"] == "READ_RECEIVED"]
        # Check a caller-provided UUID across the whole history before the
        # identical-file fast path; otherwise a later receipt's ID could be
        # accidentally reused by an earlier file's duplicate import.
        for receipt in receipts:
            same_pair = receipt.get("request_id") == request_id and receipt.get("file_id") == file_id
            if receipt_id is not None and receipt.get("receipt_id") == receipt_id and not (
                    same_pair and receipt.get("raw_hash") == raw_hash):
                raise SafetyError("READ_RECEIPT_UUID_CONFLICT")
        for receipt in receipts:
            same_pair = receipt.get("request_id") == request_id and receipt.get("file_id") == file_id
            if same_pair:
                if receipt.get("raw_hash") != raw_hash:
                    raise SafetyError("READ_RECEIPT_CONFLICT")
                evidence = journal.read_evidence(receipt["receipt_ref"])
                if digest(evidence["raw"]) != raw_hash:
                    raise SafetyError("READ_RECEIPT_HASH_MISMATCH")
                return receipt["receipt_ref"]
        receipt_id = receipt_id or str(uuid.uuid4())
        receipt = {"schema_version": 1, "request_id": request_id, "receipt_id": receipt_id,
                   "file_id": file_id, "plan_hash": prepared["plan_hash"], "raw_hash": raw_hash,
                   "raw": copy.deepcopy(raw), "read_only": True, "execution_ready": False}
        reference = journal.evidence(receipt)
        journal.append("READ_RECEIVED", payload={key: value for key, value in receipt.items() if key != "raw"}
                       | {"receipt_ref": reference})
        return reference


def reconcile_reads(journal, request_id: str) -> dict:
    """Assess immutable receipts without granting approval or changing Drive."""
    with journal.locked():
        prepared, records = _prepared(journal, request_id, enforce_exclusions=False)
        denied, denied_refs = _tombstones(records)
        body = journal.read_evidence(prepared["plan_ref"])
        excluded_after_prepare = False
        try:
            _read_targets(body, denied | set(prepared["denied_ids"]), denied_refs)
        except SafetyError:
            excluded_after_prepare = True
        receipts = [record["payload"] for record in records
                    if record["kind"] == "READ_RECEIVED" and record["payload"].get("request_id") == request_id]
        assessments = {}
        for received in receipts:
            file_id = received["file_id"]
            if file_id not in prepared["expected_file_ids"] or file_id in assessments:
                raise SafetyError("READ_RECEIPT_SET_CONFLICT")
            evidence = journal.read_evidence(received["receipt_ref"])
            if (evidence.get("request_id") != request_id or evidence.get("file_id") != file_id
                    or evidence.get("plan_hash") != prepared["plan_hash"]
                    or digest(evidence.get("raw")) != received["raw_hash"]):
                raise SafetyError("READ_RECEIPT_BINDING_MISMATCH")
            metadata = _metadata(evidence["raw"])
            if metadata.get("id") != file_id:
                raise SafetyError("READ_RECEIPT_ID_MISMATCH")
            assessment = assess_connector_metadata(metadata)
            # Full normalized snapshots are useful fixtures; validity never
            # implies that an actual connector provides all required evidence.
            try:
                snapshot(metadata)
                valid_snapshot = True
            except (SafetyError, KeyError, TypeError, AttributeError):
                valid_snapshot = False
            assessments[file_id] = {"receipt_ref": received["receipt_ref"],
                                    "valid_snapshot": valid_snapshot,
                                    "assessment": assessment,
                                    "read_only": True, "execution_ready": False}
        received_ids = sorted(assessments)
        missing_ids = sorted(set(prepared["expected_file_ids"]) - set(received_ids))
        reasons = []
        if missing_ids:
            reasons.append("MISSING_RECEIPTS")
        if any(not value["valid_snapshot"] for value in assessments.values()):
            reasons.append("INCOMPLETE_METADATA_OR_ACCESS")
        if excluded_after_prepare:
            reasons.append("EXCLUDED_AFTER_PREPARE")
        report = {"schema_version": 1, "request_id": request_id,
                  "plan_hash": prepared["plan_hash"], "received_file_ids": received_ids,
                  "missing_file_ids": missing_ids, "assessments": assessments,
                  "state": "READ_ONLY_BLOCKED" if reasons else "READ_ONLY_METADATA_COMPLETE",
                  "reason_codes": reasons, "read_only": True, "execution_ready": False,
                  "limitations": ["No connector invocation was performed by this module",
                                  "Receipts do not authorize or prove any mutation",
                                  "Live execution and approval remain disabled"]}
        report_ref = journal.evidence(report)
        journal.append("READ_RECONCILED", payload={"request_id": request_id,
                       "plan_hash": prepared["plan_hash"], "report_ref": report_ref,
                       "state": report["state"], "reason_codes": reasons,
                       "read_only": True, "execution_ready": False})
        return report | {"report_ref": report_ref}
