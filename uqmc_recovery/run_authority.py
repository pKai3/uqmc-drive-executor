"""Authority validated once per locked run, with fail-closed dispatch checks.

This is a process-local optimization, not an approval store. The caller owns a
owned plan snapshot that the runner never modifies, plus the journal's outer
controller lock for this object's
entire lifetime. No authority or plan change is adopted by ``check()``.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import os
import re
import stat
import threading

from .model import SafetyError
from .section_importer import validate_section_authority


_REFERENCE = re.compile(r"sha256:([0-9a-f]{64})\Z")
_REVOCATIONS = {"REVOKED", "SECTION_APPROVAL_REVOKED", "SECTION_APPROVAL_SUPERSEDED"}
_BINDINGS = {"SECTION_IMPORTED", "HANDOFF_AUTHORIZED", "APPROVED", "DRAFTED",
             "HANDOFF_DISPATCH_ENABLED", "HANDOFF_DISPATCH_DISABLED"}


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise SafetyError("RUN_AUTHORITY_TIME_INVALID") from None


def _fingerprint(path):
    try:
        value = path.lstat()
    except OSError:
        raise SafetyError("RUN_AUTHORITY_EVIDENCE_CHANGED") from None
    if not stat.S_ISREG(value.st_mode):
        raise SafetyError("RUN_AUTHORITY_EVIDENCE_CHANGED")
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_mode, value.st_nlink)


class _EvidenceTracker:
    """Delegate validation while remembering every evidence file it reads."""

    def __init__(self, journal):
        self.journal = journal
        self.fingerprints = {}

    def __getattr__(self, name):
        return getattr(self.journal, name)

    def read_evidence(self, reference):
        match = _REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
        if match is None:
            raise SafetyError("RUN_AUTHORITY_EVIDENCE_REFERENCE_INVALID")
        path = self.journal.evidence_dir / (match.group(1) + ".json")
        before = _fingerprint(path)
        if path in self.fingerprints and self.fingerprints[path] != before:
            raise SafetyError("RUN_AUTHORITY_EVIDENCE_CHANGED")
        value = self.journal.read_evidence(reference)
        after = _fingerprint(path)
        if before != after:
            raise SafetyError("RUN_AUTHORITY_EVIDENCE_CHANGED")
        self.fingerprints[path] = after
        return value

    def check(self):
        for path, expected in self.fingerprints.items():
            if _fingerprint(path) != expected:
                raise SafetyError("RUN_AUTHORITY_EVIDENCE_CHANGED")


class RunAuthority:
    """One shared cache for one plan and one outer journal-lock acquisition.

    Construct under ``journal.locked()`` or ``journal.shared_locked()``. Share
    this same object with worker runners. ``check()`` returns a defensive copy
    of the frozen authorization and never reauthorizes a changed run. Any failed
    check permanently poisons this instance; explicitly construct a fresh object
    for a subsequent run so full validation occurs again.
    """

    def __init__(self, journal, plan, plan_sha256, audit_permissions=True):
        if type(audit_permissions) is not bool:
            raise SafetyError("EXPLICIT_PERMISSION_AUDIT_BOOLEAN_REQUIRED")
        if not isinstance(plan_sha256, str) or plan.proposal_hash != plan_sha256:
            raise SafetyError("RUN_AUTHORITY_PLAN_HASH_MISMATCH")
        self.journal = journal
        self._gate = threading.RLock()
        self._pid = os.getpid()
        self._epoch = getattr(journal, "_lock_epoch", None)
        self._failed = None
        self._require_lifetime()
        self.plan_sha256 = plan_sha256
        self.run_id, self.batch_id = plan.run_id, plan.batch_id
        self.proposal_revision = plan.proposal_revision
        self._source_ids, self._proposal_refs, self._operation_ids = set(), set(), set()
        for operation in plan.operations:
            self._source_ids |= {operation.file_id} | set(operation.before["parents"]) | set(operation.target.get("parents", []))
            self._source_ids |= {member["id"] for member in (operation.before.get("subtree") or {}).get("members", [])}
            self._source_ids |= set(operation.allowed_collision_ids)
            self._proposal_refs |= set(operation.proposal_refs)
            if operation.action == "MOVE" and len(operation.proposal_refs) == 2:
                self._proposal_refs.add("/".join(operation.proposal_refs))
            self._operation_ids.add(operation.operation_id)
        self._evidence = _EvidenceTracker(journal)
        with journal.locked():
            # The only full authority validation during this cache's lifetime.
            self.section_authority = copy.deepcopy(validate_section_authority(self._evidence, plan))
            records = journal.records()
            authorizations = [event for event in records if event["kind"] == "HANDOFF_AUTHORIZED"
                              and event["payload"].get("proposal_hash") == plan_sha256]
            if not authorizations:
                raise SafetyError("EXISTING_SECTION_AUTHORIZATION_REQUIRED")
            self._handoff = copy.deepcopy(authorizations[-1]["payload"])
            self._authorization = self._evidence.read_evidence(self._handoff["authorization_ref"])
            if self._authorization.get("authorized") is not True or self._authorization.get("proposal_hash") != plan_sha256:
                raise SafetyError("AUTHORIZATION_MISMATCH")
            if not audit_permissions and self._authorization.get("access_policy") != "SCOPED_EXPOSURE_ACCEPTED":
                raise SafetyError("SKIP_PERMISSION_AUDIT_REQUIRES_ACCEPTED_SECTION_EXPOSURE")
            self._approval_ids = {self._handoff.get("approval_id")} - {None}
            self._approval_refs = {self._handoff.get("approval_ref"), self._handoff["authorization_ref"]} - {None}
            self._section_id = None
            self._section_expiry = None
            self._batch_approved_at = None
            self._batch_expiry = None
            if self.section_authority is not None:
                self._section_id = self.section_authority["section_id"]
                self._approval_ids.add(self.section_authority["owner_approval_id"])
                self._approval_refs |= {self.section_authority["owner_approval_ref"], self.section_authority["section_manifest_ref"]}
                value = self.section_authority.get("expires_at_utc")
                self._section_expiry = _timestamp(value) if value is not None else None
                owner = self._evidence.read_evidence(self.section_authority["owner_approval_ref"])
                self._proposal_refs.update(owner.get("operation_refs", []))
            if self._handoff.get("approval_ref"):
                approval = self._evidence.read_evidence(self._handoff["approval_ref"])
                if (approval.get("approval_id") != self._handoff.get("approval_id")
                        or approval.get("proposal_hash") != plan_sha256):
                    raise SafetyError("AUTHORIZATION_MISMATCH")
                self._batch_approved_at = _timestamp(approval["approved_at"])
                self._batch_expiry = _timestamp(approval["expires_at"])
            self._check_expiry()
            for event in records:
                self._check_cancellation(event)
            self._evidence.check()
            self._cursor = records[-1]["seq"] if records else 0

    def _require_lifetime(self):
        if (self._pid != os.getpid() or self._epoch is None
                or getattr(self.journal, "_depth", 0) == 0
                or getattr(self.journal, "_lock_epoch", None) != self._epoch):
            raise SafetyError("RUN_AUTHORITY_LOCK_LIFETIME_ENDED")

    def _check_expiry(self):
        now = datetime.now(timezone.utc)
        if self._section_expiry is not None and now >= self._section_expiry:
            raise SafetyError("SECTION_APPROVAL_EXPIRED")
        if ((self._batch_approved_at is not None and now < self._batch_approved_at)
                or (self._batch_expiry is not None and now >= self._batch_expiry)):
            raise SafetyError("APPROVAL_EXPIRED_OR_FUTURE")

    def _check_cancellation(self, event):
        kind, payload = event["kind"], event["payload"]
        if kind in _REVOCATIONS and payload.get("approval_id") in self._approval_ids:
            if self.section_authority and payload.get("approval_id") == self.section_authority["owner_approval_id"]:
                raise SafetyError("SECTION_APPROVAL_REVOKED_OR_SUPERSEDED")
            raise SafetyError("APPROVAL_REVOKED")
        if kind == "EXCLUDED" and payload.get("file_id") in self._source_ids:
            raise SafetyError("EXCLUDED")
        if kind == "PROPOSAL_EXCLUDED" and payload.get("proposal_ref") in self._proposal_refs:
            raise SafetyError("PROPOSAL_EXCLUDED")

    def _binding_changed(self, event):
        if event["kind"] not in _BINDINGS:
            return False
        payload = event["payload"]
        if (payload.get("proposal_hash") == self.plan_sha256
                or payload.get("run_id") == self.run_id or payload.get("batch_id") == self.batch_id
                or payload.get("approval_id") in self._approval_ids
                or payload.get("owner_approval_id") in self._approval_ids
                or any(payload.get(key) in self._approval_refs for key in
                       ("approval_ref", "authorization_ref", "owner_approval_ref", "manifest_ref"))
                or (self._section_id is not None and payload.get("section_id") == self._section_id)):
            return True
        if event["kind"] in {"HANDOFF_DISPATCH_ENABLED", "HANDOFF_DISPATCH_DISABLED"}:
            return payload.get("controller_id") == self._handoff.get("controller_id")
        if event["kind"] == "SECTION_IMPORTED" and self._section_id is not None:
            # A replacement section can have a new batch and hash. Prove that a
            # new import is unrelated rather than silently retaining old scope.
            if not payload.get("manifest_ref"):
                return True
            evidence = self._evidence.read_evidence(payload["manifest_ref"])
            return evidence.get("projection", {}).get("section_id") == self._section_id
        return False

    def check(self):
        with self._gate:
            if self._failed is not None:
                raise SafetyError(self._failed)
            try:
                self._require_lifetime()
                with self.journal.locked():
                    self._require_lifetime()
                    self._check_expiry()
                    self._evidence.check()
                    tail = self.journal.records_after(self._cursor)
                    for event in tail:
                        self._check_cancellation(event)
                        if self._binding_changed(event):
                            raise SafetyError("RUN_AUTHORITY_BINDING_CHANGED_REINITIALIZE")
                    if tail:
                        self._cursor = tail[-1]["seq"]
                    return copy.deepcopy(self._authorization)
            except Exception as error:
                self._failed = str(error) or "RUN_AUTHORITY_CHECK_FAILED"
                raise
