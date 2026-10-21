"""Standalone desired-state runner over an existing approved frozen plan.

No assistant action tickets, global rename phase, automatic rollback or OAuth
flow. Historical request uncertainty and observed current state are distinct.
"""
from __future__ import annotations

import argparse
import copy
from collections import Counter
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import random
import threading
import time
import uuid

from .__main__ import plan_from_json, save_new
from .direct_transport import GoogleDriveTransport, TransportError
from .journal import Journal
from .model import FOLDER, SafetyError, digest, validate_extension_evidence, validate_rename_name
from .projection import HEADERS
from .section_importer import validate_section_authority
from .run_authority import RunAuthority


VERIFIED = {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"}
LEGACY_STATES = VERIFIED | {"PREPARED", "ATTEMPTED", "UNCERTAIN", "BLOCKED_CONFLICT", "FAILED_NOT_APPLIED", "SKIPPED"}
IMMUTABLE = {"size": "size", "md5_checksum": "md5Checksum", "owners": "owners", "drive_id": "driveId"}


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def core(raw):
    if any(k not in raw for k in ("id", "name", "parents")) or not isinstance(raw["parents"], list):
        raise SafetyError("INCOMPLETE_CURRENT_STATE")
    return {"id": raw["id"], "name": raw["name"], "parents": sorted(raw["parents"]),
            "mime_type": raw.get("mimeType", raw.get("mime_type"))}


class _RunPlan:
    """An owned plan copy with one retained digest, shared by this run's workers.

    The caller's nested dictionaries must not remain aliases of the approved
    snapshot. The owned copy is never modified by the runner.
    """
    def __init__(self, plan):
        self._plan = copy.deepcopy(plan)
        self.proposal_hash = self._plan.proposal_hash

    def __getattr__(self, name):
        return getattr(self._plan, name)


def compile_items(plan, journal, *, validate_authority=True):
    """Keep exact approved effects; replace phase ordering with target-folder edges."""
    plan_sha256 = plan.proposal_hash
    records = journal.records()
    frozen = [r for r in records if r["kind"] == "DRAFTED" and r["payload"].get("proposal_hash") == plan_sha256]
    if not frozen or digest(journal.read_evidence(frozen[-1]["payload"]["plan_ref"])) != plan_sha256:
        raise SafetyError("EXISTING_FROZEN_PLAN_REQUIRED")
    if validate_authority:
        validate_section_authority(journal, plan)
    excluded = set(plan.excluded_ids) | {r["payload"]["file_id"] for r in records if r["kind"] == "EXCLUDED"}
    groups = {}
    for op in plan.ordered():
        if op.direction != "FORWARD" or op.action not in {"MOVE", "RENAME"}:
            raise SafetyError("FORWARD_MOVE_RENAME_PLAN_REQUIRED")
        if op.file_id in excluded or set(op.before["parents"]) & excluded or set(op.target.get("parents", [])) & excluded:
            raise SafetyError("EXCLUDED")
        if op.action == "RENAME":
            validate_rename_name(op, op.before)
            validate_extension_evidence(journal, op)
        item = groups.setdefault(op.file_id, {"source_id": op.file_id, "original": core(op.before),
            "target": core(op.before), "baseline": copy.deepcopy(op.before), "operations": [], "dependencies": [],
            "allowed_collision_ids": set(), "historical_verified_fields": {}, "historical_uncertain_operation_ids": []})
        if any(old.action == op.action for old in item["operations"]):
            raise SafetyError("AMBIGUOUS_REPEATED_FIELD_OPERATION")
        item["target"].update(copy.deepcopy(op.target))
        item["target"]["parents"] = sorted(item["target"]["parents"])
        item["operations"].append(op)
        item["allowed_collision_ids"].update(op.allowed_collision_ids)
    latest = {r["operation_id"]: r for r in records if r["kind"] in LEGACY_STATES}
    for item in groups.values():
        if item["source_id"] in item["target"]["parents"]:
            raise SafetyError("SELF_PARENT")
        for op in item["operations"]:
            event = latest.get(op.operation_id)
            if event and event["kind"] in VERIFIED:
                field = "name" if op.action == "RENAME" else "parents"
                item["historical_verified_fields"][field] = op.target[field]
            if event and event["kind"] in {"ATTEMPTED", "UNCERTAIN"}:
                item["historical_uncertain_operation_ids"].append(op.operation_id)
        item["dependencies"] = [p for p in item["target"]["parents"] if p in groups and groups[p]["target"]["mime_type"] == FOLDER]
    # A folder must not mutate concurrently with an original descendant that
    # leaves it. Target-parent edges alone miss those outgoing children. Honor
    # a required inversion (child leaves first, old parent moves into child)
    # instead of introducing a cycle or inventing a global phase.
    def depends(source, ancestor, seen=None):
        seen = set() if seen is None else seen
        if source in seen:
            return False
        seen.add(source)
        return any(d == ancestor or depends(d, ancestor, seen)
                   for d in groups[source]["dependencies"])
    for folder in groups.values():
        if folder["original"]["mime_type"] != FOLDER:
            continue
        descendants = {m["id"] for m in (folder["baseline"].get("subtree") or {}).get("members", [])}
        descendants |= {s for s, child in groups.items() if folder["source_id"] in child["original"]["parents"]}
        for source in sorted(descendants & groups.keys()):
            if not depends(folder["source_id"], source) and folder["source_id"] not in groups[source]["dependencies"]:
                groups[source]["dependencies"].append(folder["source_id"])
    ordered, pending = [], list(groups.values())
    while pending:
        ready = [i for i in pending if set(i["dependencies"]) <= {x["source_id"] for x in ordered}]
        if not ready:
            raise SafetyError("TARGET_FOLDER_CYCLE")
        for item in ready:
            ordered.append(item)
            pending.remove(item)
    for index, item in enumerate(ordered, 1):
        item["operation_order"] = index
    return ordered


class DesiredRunner:
    def __init__(self, journal, plan, transport, *, max_attempts=3, retry_delay=1, fault=None, progress=None, audit_permissions=True):
        if not 1 <= max_attempts <= 5:
            raise SafetyError("MAX_ATTEMPTS_MUST_BE_1_TO_5")
        self.journal, self.plan, self.transport = journal, _RunPlan(plan), transport
        self.plan_sha256 = self.plan.proposal_hash
        self._run_authority = None
        if type(audit_permissions) is not bool:
            raise SafetyError("EXPLICIT_PERMISSION_AUDIT_BOOLEAN_REQUIRED")
        self.audit_permissions = audit_permissions
        self.max_attempts, self.retry_delay = max_attempts, retry_delay
        self.fault = fault or (lambda *args: None)
        self._stop_event = threading.Event()
        self._progress_gate = threading.RLock()
        self.progress = progress or (lambda event: None)
        self.progress_output_failed = False
        self.last_backup_path = None
        self.last_backup_manifest = None
        self.items = compile_items(self.plan, journal, validate_authority=False)
        self.by_id = {i["source_id"]: i for i in self.items}

    @property
    def stop_requested(self):
        return self._stop_event.is_set()

    @stop_requested.setter
    def stop_requested(self, value):
        self._stop_event.set() if value else self._stop_event.clear()

    def report_progress(self, event, **value):
        with self._progress_gate:
            try:
                self.progress({"event": event, **value})
            except Exception:
                # An output failure must not turn a verified action into a retry.
                self.progress_output_failed = True

    def snapshot_backup(self, path, label):
        started = time.monotonic()
        self.report_progress('stage_started', stage_name=label)
        outcome = 'FAILED'
        try:
            manifest = self.journal.backup(path)
            outcome = 'COMPLETE'
            return manifest
        finally:
            self.report_progress('stage_finished', stage_name=label,
                duration_seconds=round(time.monotonic() - started, 3), outcome=outcome)

    @staticmethod
    def transient(error):
        return isinstance(error, (TimeoutError, ConnectionError)) or getattr(error, "transient", False)

    def backoff(self, attempt):
        cap = min(30.0, self.retry_delay * (2 ** (attempt - 1)))
        self._stop_event.wait(random.uniform(cap / 2, cap))

    def read_retry(self, fetch):
        # Reads can be retried even after cancellation to reconcile in-flight work.
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fetch()
            except Exception as exc:
                if not self.transient(exc) or attempt == self.max_attempts:
                    raise
                self.backoff(attempt)

    def children(self, parent_id):
        return self.read_retry(lambda: self.transport.list_children(parent_id))

    def events(self, source_id=None):
        return [r for r in self.journal.records() if r["kind"].startswith("DESIRED_")
                and r["payload"].get("plan_sha256") == self.plan_sha256
                and (source_id is None or r["payload"].get("source_id") == source_id)]

    def record(self, kind, item, *, direction="FORWARD", attempt=0, **payload):
        return self.journal.append("DESIRED_" + kind,
            operation_id="desired:" + self.plan_sha256[:12] + ":" + item["source_id"] + ":" + direction,
            attempt=attempt, payload={"plan_sha256": self.plan_sha256, "run_id": self.plan.run_id,
                "source_id": item["source_id"], "direction": direction,
                "operation_order": item["operation_order"], "dependencies": item["dependencies"],
                "original_operation_ids": [o.operation_id for o in item["operations"]],
                "historical_uncertain_operation_ids": item["historical_uncertain_operation_ids"], **payload,
                "audit_permissions": self.audit_permissions})

    def read(self, source_id):
        raw = self.read_retry(lambda: self.transport.get_file(source_id))
        if raw.get("id") != source_id or raw.get("trashed") is True:
            raise SafetyError("SOURCE_ID_OR_TRASH_CONFLICT")
        core(raw)
        if self.audit_permissions:
            raw["_permissions"] = sorted(self.read_retry(lambda: self.transport.list_permissions(source_id)), key=digest)
        return raw

    @contextmanager
    def authority_session(self):
        """Full immutable approval validation once inside the controller lock."""
        if self._run_authority is not None:
            raise SafetyError("AUTHORITY_SESSION_ALREADY_ACTIVE")
        self._run_authority = RunAuthority(self.journal, self.plan, self.plan_sha256,
                                          audit_permissions=self.audit_permissions)
        try:
            yield
        finally:
            self._run_authority = None

    def authority(self):
        # Direct transition callers retain validation without a persistent cache.
        # Normal preview/apply/resume/rollback share one locked run session.
        if self._run_authority is not None:
            return self._run_authority.check()
        with self.journal.locked():
            return RunAuthority(self.journal, self.plan, self.plan_sha256,
                                audit_permissions=self.audit_permissions).check()

    def latest_change(self, item):
        changes = [r for r in self.events(item["source_id"]) if r["kind"] == "DESIRED_APPLIED"]
        rolled = {r["payload"].get("forward_event_id") for r in self.events(item["source_id"]) if r["kind"] == "DESIRED_ROLLED_BACK"}
        return next((r for r in reversed(changes) if r["event_id"] not in rolled), None)

    def _version_only_match(self, item, before, observed):
        """Accept only an explicit historical counter advance, never live drift."""
        if (core(before) != core(observed) or
                core(observed)["name"] != item["target"]["name"] or
                core(observed)["parents"] != item["original"]["parents"] or
                core(observed)["parents"] == item["target"]["parents"] or
                core(observed)["mime_type"] != item["original"]["mime_type"]):
            raise SafetyError("VERSION_REBASELINE_REQUIRES_RECORDED_RENAME_PENDING_MOVE")
        self.unchanged(before, observed)
        for key in ("modifiedTime", "_http_etag"):
            if key in before and before[key] != observed.get(key):
                raise SafetyError("VERSION_REBASELINE_SUBSTANTIVE_DRIFT")
        for key in ("canRename", "canAddChildren", "canMoveItemWithinDrive", "canMoveItemOutOfDrive"):
            if (key in before.get("capabilities", {}) and
                    before["capabilities"][key] != observed.get("capabilities", {}).get(key)):
                raise SafetyError("VERSION_REBASELINE_CAPABILITY_DRIFT")
        old, new = before.get("version"), observed.get("version")
        if (not isinstance(old, str) or not old.isdecimal() or
                not isinstance(new, str) or not new.isdecimal() or int(new) <= int(old)):
            raise SafetyError("VERSION_REBASELINE_REQUIRES_VERSION_ADVANCE")

    def _applied_baseline(self, item, active):
        historical = self.journal.read_evidence(active["payload"]["applied_ref"])
        if item["original"]["mime_type"] == FOLDER:
            reconciled = [r for r in self.events(item["source_id"])
                if r["kind"] == "DESIRED_FOLDER_RECONCILED" and r["seq"] > active["seq"]
                and r["payload"].get("forward_event_id") == active["event_id"]]
            if reconciled:
                payload = reconciled[-1]["payload"]
                if payload.get("historical_applied_ref") != active["payload"]["applied_ref"]:
                    raise SafetyError("FOLDER_RECONCILIATION_HISTORY_MISMATCH")
                accepted = self.journal.read_evidence(payload["observed_ref"])
                self._same_folder_fields(historical, accepted)
                return accepted
        accepted = [r for r in self.events(item["source_id"])
                    if r["kind"] == "DESIRED_REBASELINED" and
                    r["seq"] > active["seq"] and
                    r["payload"].get("forward_event_id") == active["event_id"]]
        if not accepted:
            return historical
        event = accepted[-1]["payload"]
        if event.get("historical_applied_ref") != active["payload"]["applied_ref"]:
            raise SafetyError("VERSION_REBASELINE_HISTORY_MISMATCH")
        observed = self.journal.read_evidence(event["observed_ref"])
        self._version_only_match(item, historical, observed)
        return observed

    def _folder_graph(self):
        """Frozen membership plus verified changes; no editable proposal reads."""
        states = {}
        for known in self.items:
            for member in (known["baseline"].get("subtree") or {}).get("members", []):
                states.setdefault(member["id"], copy.deepcopy(member))
        for known in self.items:
            states[known["source_id"]] = copy.deepcopy(known["original"])
        effects = []
        for event in self.events():
            if event["kind"] in {"DESIRED_APPLIED", "DESIRED_ROLLED_BACK"} or (
                    event["kind"] == "DESIRED_OBSERVED" and event["payload"].get("state") in {"TARGET", "PARTIAL", "NOT_APPLIED"}):
                payload = event["payload"]
                ref = payload.get("applied_ref") or payload.get("observed_ref")
                if ref and payload.get("source_id") in self.by_id:
                    state = core(self.journal.read_evidence(ref))
                    if state["id"] != payload["source_id"]:
                        raise SafetyError("FOLDER_HISTORY_IDENTITY_MISMATCH")
                    states[state["id"]] = state
                    effects.append(event)
        return states, effects

    @staticmethod
    def _inside(source, folder, states, trail=None):
        if source == folder:
            return False
        trail = set() if trail is None else trail
        if source in trail:
            raise SafetyError("FOLDER_CYCLE")
        trail = trail | {source}
        return any(p == folder or (p in states and DesiredRunner._inside(p, folder, states, trail))
                   for p in states.get(source, {}).get("parents", []))

    def _check_folder_members(self, item, target=None, preview_cache=None):
        subtree = item["baseline"].get("subtree") or {}
        if subtree.get("complete") is not True:
            raise SafetyError("COMPLETE_FOLDER_MEMBERSHIP_REQUIRED")
        states, effects = self._folder_graph()
        root = item["source_id"]
        # Older snapshots containing only IDs cannot prove parent transitions.
        # Keep those fixed members required until richer evidence is available.
        fixed = {m["id"] for m in subtree.get("members", []) if "parents" not in m and m["id"] not in self.by_id}
        expected = {s for s in states if self._inside(s, root, states)} | fixed
        pending, seen, members = [root], set(), []
        while pending:
            pid = pending.pop()
            if pid in seen:
                raise SafetyError("FOLDER_CYCLE")
            seen.add(pid)
            children = self.preview_read(preview_cache, "children", pid, self.children)
            members.extend(children)
            pending.extend(c["id"] for c in children if c["mimeType"] == FOLDER)
        actual_ids = [m["id"] for m in members]
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected:
            raise SafetyError("FOLDER_MEMBERSHIP_DRIFT")
        if target and set(target["parents"]) & ({root} | expected):
            raise SafetyError("FOLDER_CYCLE")
        for member in members:
            known = states.get(member["id"], {})
            for field, api in (("name", "name"), ("mime_type", "mimeType"), ("parents", "parents")):
                value = known.get(field, known.get(api))
                actual = member.get(api)
                if field == "parents" and value is not None:
                    value, actual = sorted(value), sorted(actual or [])
                if value is not None and value != actual:
                    raise SafetyError("FOLDER_MEMBER_METADATA_DRIFT")
        return members, states, effects

    def _same_folder_fields(self, before, observed):
        if core(before) != core(observed):
            raise SafetyError("RECORDED_APPLIED_STATE_WAS_CHANGED")
        self.unchanged(before, observed)
        if before.get("capabilities") != observed.get("capabilities"):
            raise SafetyError("FOLDER_CAPABILITY_DRIFT")

    def _check_applied(self, item, active, applied, observed, *, read_only=False):
        try:
            self.unchanged(applied, observed, strict=True)
            return
        except SafetyError as error:
            if str(error) != "CONCURRENT_CHANGE" or item["original"]["mime_type"] != FOLDER:
                raise
        self._same_folder_fields(applied, observed)
        old, new = applied.get("version"), observed.get("version")
        if not (isinstance(old, str) and old.isdecimal() and isinstance(new, str)
                and new.isdecimal() and int(new) > int(old)):
            raise SafetyError("CONCURRENT_CHANGE")
        members, states, effects = self._check_folder_members(item)
        prior = [e for e in self.events(item["source_id"]) if e["kind"] == "DESIRED_FOLDER_RECONCILED"
                 and e["payload"].get("forward_event_id") == active["event_id"]]
        since = prior[-1]["seq"] if prior else active["seq"]
        root = item["source_id"]
        original = {m["id"] for m in (item["baseline"].get("subtree") or {}).get("members", [])}
        related = []
        for event in effects:
            source = event["payload"]["source_id"]
            if event["seq"] <= since or source == root or event["kind"] == "DESIRED_OBSERVED":
                continue
            before = core(self.journal.read_evidence(event["payload"]["before_ref"]))
            after = states[source]
            if (source in original or self._inside(source, root, states) or self._inside(root, source, states)
                    or root in before["parents"] or root in after["parents"]):
                related.append(event["event_id"])
        if not related:
            raise SafetyError("CONCURRENT_CHANGE")
        if read_only:
            return
        self.record("FOLDER_RECONCILED", item, forward_event_id=active["event_id"],
            historical_applied_ref=active["payload"]["applied_ref"],
            prior_ref=self.journal.evidence(applied), observed_ref=self.journal.evidence(observed),
            membership_ref=self.journal.evidence(members), related_event_ids=related,
            reason="FOLDER_COUNTER_ADVANCE_AFTER_RELATED_VERIFIED_OPERATIONS",
            limitation="Matching relevant fields and membership verified; version causation not provable.")

    def rebaseline_version_only(self, item, *, authorization_ref):
        """Explicit operator reconciliation, not an automatic exception to CAS.

        The authority reference records the human instruction and exact scope;
        its hash is not itself permission. Original before/applied evidence stays
        intact. Subsequent observe and pre-dispatch reads use the accepted version
        strictly, so another change after this observation still blocks the move.
        """
        with self.journal.locked():
            self.authority()
            authorization = self.journal.read_evidence(authorization_ref)
            if (authorization.get("schema_version") != "uqmc.version-rebaseline.authorization.v1" or
                    authorization.get("plan_sha256") != self.plan_sha256 or
                    item["source_id"] not in authorization.get("source_ids", []) or
                    not isinstance(authorization.get("instruction"), str) or
                    not authorization["instruction"].strip()):
                raise SafetyError("EXPLICIT_VERSION_REBASELINE_AUTHORITY_REQUIRED")
            if self.pending(item, "FORWARD") or self.pending(item, "ROLLBACK"):
                raise SafetyError("VERSION_REBASELINE_PENDING_ATTEMPT")
            active = self.latest_change(item)
            if not active:
                raise SafetyError("VERSION_REBASELINE_RECORDED_APPLIED_REQUIRED")
            historical = self.journal.read_evidence(active["payload"]["applied_ref"])
            observed = self.read(item["source_id"])
            self._version_only_match(item, historical, observed)
            return self.record("REBASELINED", item,
                forward_event_id=active["event_id"],
                historical_applied_ref=active["payload"]["applied_ref"],
                historical_before_ref=active["payload"].get("before_ref"),
                observed_ref=self.journal.evidence(observed), authorization_ref=authorization_ref,
                old_version=historical["version"], accepted_version=observed["version"],
                reason="EXPLICIT_HISTORICAL_VERSION_ONLY_RECONCILIATION",
                comparison="Known identity/name/parents/content/owners/capabilities unchanged; version causation unknown")

    def approved(self, item, raw, *, dependency=False, read_only=False):
        current = core(raw)
        if current["id"] != item["source_id"] or current["mime_type"] != item["original"]["mime_type"]:
            raise SafetyError("IDENTITY_OR_MIME_DRIFT")
        for field in ("name", "parents"):
            if current[field] not in (item["original"][field], item["target"][field]):
                raise SafetyError("UNAPPROVED_" + field.upper() + "_DRIFT")
            known = item["historical_verified_fields"].get(field)
            if known is not None and current[field] != known:
                raise SafetyError("HISTORICAL_VERIFIED_EFFECT_WAS_CHANGED")
        active = self.latest_change(item)
        baseline = item["baseline"]
        if active:
            applied = self._applied_baseline(item, active)
            pending = self.pending(item, "FORWARD")
            intended = self.journal.read_evidence(pending["payload"]["target_ref"]) if pending else None
            if pending and core(raw) == intended:
                prior = self.journal.read_evidence(pending["payload"]["before_ref"])
                self.unchanged(prior, raw, moving=core(prior)["parents"] != intended["parents"])
            else:
                if core(raw) != core(applied):
                    raise SafetyError("RECORDED_APPLIED_STATE_WAS_CHANGED")
                # Parent observations authorize use of the current location;
                # siblings/provider propagation may advance its version without
                # changing that location. Actual mutation targets stay strict.
                if dependency:
                    self.unchanged(applied, raw)
                else:
                    self._check_applied(item, active, applied, raw, read_only=read_only)
            if dependency:
                known = applied.get("capabilities", {}).get("canAddChildren")
                if known is not None and raw.get("capabilities", {}).get("canAddChildren") != known:
                    raise SafetyError("DEPENDENCY_CAPABILITY_DRIFT")
        else:
            for old, native in IMMUTABLE.items():
                if baseline.get(old) is not None and raw.get(native) != baseline[old]:
                    raise SafetyError("KNOWN_CONTENT_OR_OWNER_DRIFT")
            # When auditing, retain every historically known permission field
            # at original parents. An explicit accepted no-audit mode skips it.
            if self.audit_permissions and current["parents"] == item["original"]["parents"] and baseline.get("permissions") is not None:
                old = baseline["permissions"]
                keys = set().union(*(set(p) for p in old)) if old else set()
                projected = [{k: p.get(k) for k in keys} for p in raw["_permissions"]]
                if sorted(old, key=digest) != sorted(projected, key=digest):
                    raise SafetyError("KNOWN_PERMISSION_DRIFT")
        return current

    def unchanged(self, before, after, *, strict=False, moving=False):
        for key in ("id", "mimeType", "size", "md5Checksum", "owners", "driveId", "shortcutDetails", "trashed"):
            if key in before and before[key] != after.get(key):
                raise SafetyError("PROTECTED_METADATA_DRIFT")
        if self.audit_permissions and not moving and before.get("_permissions") != after.get("_permissions"):
            raise SafetyError("PERMISSION_DRIFT")
        if self.audit_permissions and moving:
            current = {p.get("id"): p for p in after.get("_permissions", [])}
            for p in before.get("_permissions", []):
                if any(d.get("inherited") is False for d in p.get("permissionDetails", [])):
                    if any(current.get(p.get("id"), {}).get(k) != p.get(k) for k in ("id", "type", "role", "emailAddress", "domain", "allowFileDiscovery")):
                        raise SafetyError("DIRECT_PERMISSION_DRIFT")
        if strict:
            for key in ("version", "modifiedTime", "_http_etag"):
                if key in before and before[key] != after.get(key):
                    raise SafetyError("CONCURRENT_CHANGE")

    @staticmethod
    def delta(before, target):
        current = core(before)
        args = {"fileId": current["id"]}
        if current["name"] != target["name"]:
            args["name"] = target["name"]
        added = set(target["parents"]) - set(current["parents"])
        removed = set(current["parents"]) - set(target["parents"])
        if added:
            args["addParents"] = ",".join(sorted(added))
        if removed:
            args["removeParents"] = ",".join(sorted(removed))
        return args

    @staticmethod
    def preview_read(cache, kind, identity, fetch):
        """Cache successful auxiliary reads only in one read-only preview."""
        if cache is None:
            return fetch(identity)
        key = (kind, identity)
        if key not in cache:
            cache[key] = copy.deepcopy(fetch(identity))
        return copy.deepcopy(cache[key])

    def preflight(self, item, before, target, *, rollback=False, preview_cache=None):
        parents = {}
        if before["name"] != target["name"] and before.get("capabilities", {}).get("canRename") is False:
            raise SafetyError("SOURCE_CANNOT_RENAME")
        for pid in sorted(set(before["parents"]) | set(target["parents"])):
            parent = self.preview_read(preview_cache, "parent", pid, self.read)
            if parent.get("mimeType") != FOLDER:
                raise SafetyError("DESTINATION_NOT_FOLDER")
            if pid in self.by_id:
                self.approved(self.by_id[pid], parent, dependency=True)
                if not rollback and pid in item["dependencies"] and core(parent) != self.by_id[pid]["target"]:
                    raise SafetyError("PARENT_DEPENDENCY_LOCATION_CONFLICT")
            parents[pid] = parent
        for pid in target["parents"]:
            if pid not in before["parents"]:
                destination = parents[pid]
                if destination.get("capabilities", {}).get("canAddChildren") is False:
                    raise SafetyError("DESTINATION_CANNOT_ADD_CHILDREN")
                capability = "canMoveItemWithinDrive" if before.get("driveId") == destination.get("driveId") else "canMoveItemOutOfDrive"
                if before.get("capabilities", {}).get(capability) is False:
                    raise SafetyError("SOURCE_CANNOT_MOVE_" + capability)
            siblings = self.preview_read(preview_cache, "children", pid, self.children)
            collision = {s["id"] for s in siblings if s["id"] != item["source_id"] and s["name"].casefold() == target["name"].casefold()}
            if collision - item["allowed_collision_ids"]:
                raise SafetyError("UNAPPROVED_SIBLING_COLLISION")
        if core(before)["parents"] != target["parents"]:
            if not rollback and self.authority().get("access_policy") != "SCOPED_EXPOSURE_ACCEPTED":
                raise SafetyError("MOVE_ACCESS_REVIEW_REQUIRED")
            if before.get("mimeType") == FOLDER:
                self._check_folder_members(item, target, preview_cache)
        return parents

    def observe(self, item, *, dependency=False, read_only=False):
        raw = self.read(item["source_id"])
        current = self.approved(item, raw, dependency=dependency, read_only=read_only)
        partial = any(current[f] == item["target"][f] != item["original"][f] for f in ("name", "parents"))
        return raw, current, "TARGET" if current == item["target"] else "PARTIAL" if partial else "NOT_APPLIED"

    def preview(self, *, limit=None):
        with self.journal.locked(), self.authority_session():
            self.authority()
            output = []
            # Local to this invocation: apply/resume/rollback never receive it.
            # Source observations always bypass this advisory preview cache.
            preview_cache = {}
            for item in self.items:
                if self.stop_requested or (limit is not None and len(output) >= limit):
                    break
                self.report_progress("item_started", source_id=item["source_id"], mode="PREVIEW",
                    completed_items=len(output), total_items=len(self.items))
                try:
                    raw, current, state = self.observe(item, read_only=True)
                    if raw.get("mimeType") == FOLDER:
                        preview_cache[("parent", item["source_id"])] = copy.deepcopy(raw)
                    parents = self.preflight(item, raw, item["target"], preview_cache=preview_cache)
                    output.append({"source_id": item["source_id"], "state": state, "observed": current,
                        "access": {"source_capabilities": raw.get("capabilities"),
                            "parent_capabilities": {pid: value.get("capabilities") for pid, value in parents.items()},
                            "missing_capabilities_mean": "UNVERIFIED"},
                        "effects": self.effects(item, current),
                        "required_update": self.delta(raw, item["target"]), "dependencies": item["dependencies"],
                        "historical_uncertain_operation_ids": item["historical_uncertain_operation_ids"]})
                except Exception as exc:
                    output.append(self.error_item(item, exc))
                self.report_progress("item_finished", completed_items=len(output), total_items=len(self.items), **output[-1])
            return self.summary(output, preview=True)

    @staticmethod
    def error_item(item, exc):
        return {"source_id": item["source_id"], "state": "UNAVAILABLE" if isinstance(exc, (TransportError, TimeoutError, ConnectionError)) else "CONFLICT",
                "reason": str(exc) if isinstance(exc, SafetyError) else type(exc).__name__,
                **({"request_error": exc.as_dict()} if isinstance(exc, TransportError) else {})}

    @staticmethod
    def effects(item, current):
        return {label: "NOT_REQUIRED" if item["original"][field] == item["target"][field] else
                "APPLIED" if current[field] == item["target"][field] else "NOT_APPLIED"
                for field, label in (("name", "rename"), ("parents", "move"))}

    def pending(self, item, direction):
        events = [r for r in self.events(item["source_id"]) if r["payload"].get("direction") == direction]
        intents = [r for r in events if r["kind"] == "DESIRED_ATTEMPTED"]
        if not intents:
            return None
        last = intents[-1]
        finished = [r for r in events if r["seq"] > last["seq"] and r["kind"] in {"DESIRED_APPLIED", "DESIRED_ROLLED_BACK"}]
        return None if finished else last

    def transition(self, item, before, target, *, rollback_event=None):
        direction = "ROLLBACK" if rollback_event else "FORWARD"
        pending = self.pending(item, direction)
        recovery_before = self.journal.read_evidence(pending["payload"]["before_ref"]) if pending else before
        if pending and core(before) == target:
            self.unchanged(recovery_before, before, moving=core(recovery_before)["parents"] != target["parents"])
            return self.applied(item, recovery_before, before, pending["payload"], rollback_event, recovered=True)
        if pending:
            if core(before) != core(recovery_before):
                raise SafetyError("INTERRUPTED_ATTEMPT_STATE_CONFLICT")
            self.unchanged(recovery_before, before, strict=True)
        for attempt in range(1, self.max_attempts + 1):
            if self.stop_requested:
                return {"source_id": item["source_id"], "state": "NOT_APPLIED", "reason": "PAUSED_BEFORE_DISPATCH"}
            self.authority()
            parents = self.preflight(item, before, target, rollback=bool(rollback_event))
            args = self.delta(before, target)
            payload = {"before_ref": self.journal.evidence(before), "target_ref": self.journal.evidence(target),
                "args": args, "parents_ref": self.journal.evidence(parents), "attempt_id": str(uuid.uuid4()),
                "forward_event_id": rollback_event["event_id"] if rollback_event else None,
                "concurrency": "IF_MATCH_HEADER_UNVERIFIED_LIVE" if before.get("_http_etag") else "FRESH_READ_RACE_REMAINS"}
            self.record("PREPARED", item, direction=direction, attempt=attempt, **payload)
            fresh = self.read(item["source_id"])
            if core(fresh) != core(before):
                raise SafetyError("CONCURRENT_NAME_OR_PARENT_CHANGE")
            self.unchanged(before, fresh, strict=True)
            if self.stop_requested:
                return {"source_id": item["source_id"], "state": "NOT_APPLIED", "reason": "PAUSED_BEFORE_DISPATCH"}
            self.authority()  # Cheap live revocation/expiry check immediately before dispatch.
            self.record("ATTEMPTED", item, direction=direction, attempt=attempt, **payload)
            self.fault("before_dispatch", item)
            error = None
            try:
                response = self.transport.update_metadata(args, etag=fresh.get("_http_etag"))
                self.fault("after_dispatch", item)
                self.record("RECEIPT", item, direction=direction, attempt=attempt,
                    response_ref=self.journal.evidence(response), request_outcome="RETURNED_SUCCESS", **payload)
            except Exception as exc:
                error = exc
                self.report_progress("request_failed", source_id=item["source_id"], direction=direction,
                    request_error=exc.as_dict() if isinstance(exc, TransportError) else {"code": type(exc).__name__},
                    next_step="READ_CURRENT_STATE_BEFORE_ANY_RETRY")
                self.record("RECEIPT", item, direction=direction, attempt=attempt,
                    request_outcome="UNKNOWN", error_type=type(exc).__name__, http_status=getattr(exc, "status", None),
                    request_error=exc.as_dict() if isinstance(exc, TransportError) else {"code": type(exc).__name__}, **payload)
            observed = self.read(item["source_id"])
            self.fault("after_readback", item)
            self.unchanged(before, observed, moving=core(before)["parents"] != target["parents"])
            if core(observed) == target:
                return self.applied(item, before, observed, payload, rollback_event, recovered=bool(error))
            if core(observed) != core(before):
                raise SafetyError("UNAPPROVED_POST_WRITE_STATE")
            self.unchanged(before, observed, strict=True)
            self.record("OBSERVED", item, direction=direction, attempt=attempt, observed_ref=self.journal.evidence(observed),
                state="NOT_APPLIED", request_outcome="UNKNOWN" if error else "RETURNED_SUCCESS", **payload)
            transient = self.transient(error)
            if not transient or attempt == self.max_attempts:
                return {"source_id": item["source_id"], "state": "NOT_APPLIED", "reason": "RETRY_BUDGET_EXHAUSTED" if transient else "REQUEST_REFUSED_OR_UNEXPECTED_READBACK"}
            self.backoff(attempt)
            if self.stop_requested:
                return {"source_id": item["source_id"], "state": "NOT_APPLIED", "reason": "PAUSED_AFTER_RECONCILIATION"}
            before = self.read(item["source_id"])
            if core(before) == target:
                return self.applied(item, recovery_before, before, payload, rollback_event, recovered=True)
            if core(before) != core(observed):
                raise SafetyError("STATE_CHANGED_BEFORE_RETRY")
            self.unchanged(observed, before, strict=True)
        raise AssertionError("unreachable")

    def applied(self, item, before, after, payload, rollback_event, *, recovered):
        direction = "ROLLBACK" if rollback_event else "FORWARD"
        kind = "ROLLED_BACK" if rollback_event else "APPLIED"
        payload = {key: value for key, value in payload.items() if key not in {
            "direction", "source_id", "run_id", "plan_sha256", "original_operation_ids",
            "historical_uncertain_operation_ids", "operation_order", "dependencies"}}
        self.record(kind, item, direction=direction, **(payload | {
            "before_ref": self.journal.evidence(before), "applied_ref": self.journal.evidence(after),
            "observed_ref": self.journal.evidence(after), "request_outcome": "UNKNOWN_RECOVERED_BY_READ" if recovered else "RETURNED_SUCCESS",
            "forward_event_id": rollback_event["event_id"] if rollback_event else None}))
        return {"source_id": item["source_id"], "state": kind, "observed": core(after),
            "effects": self.effects(item, core(after)), "historical_request_uncertainty_retained": bool(recovered or item["historical_uncertain_operation_ids"])}

    def execute_item(self, item, *, rollback=False):
        """One file at a time per worker; existing transition owns WAL/readback."""
        try:
            if rollback:
                event = self.latest_change(item)
                if not event:
                    return {"source_id": item["source_id"], "state": "NOTHING_TO_ROLL_BACK"}
                raw = self.read(item["source_id"])
                applied = self._applied_baseline(item, event)
                target = core(self.journal.read_evidence(event["payload"]["before_ref"]))
                pending = self.pending(item, "ROLLBACK")
                if pending and core(raw) == target:
                    return self.transition(item, raw, target, rollback_event=event)
                if core(raw) != core(applied):
                    raise SafetyError("ROLLBACK_CURRENT_STATE_CONFLICT")
                self._check_applied(item, event, applied, raw)
                return self.transition(item, raw, target, rollback_event=event)
            raw, current, state = self.observe(item)
            if state == "TARGET" and self.pending(item, "FORWARD"):
                return self.transition(item, raw, item["target"])
            if state == "TARGET":
                if item["original"]["mime_type"] == FOLDER:
                    self._check_folder_members(item)
                self.record("OBSERVED", item, observed_ref=self.journal.evidence(raw), state="TARGET")
                return {"source_id": item["source_id"], "state": "TARGET", "observed": current,
                        "effects": self.effects(item, current)}
            return self.transition(item, raw, item["target"])
        except Exception as exc:
            result = self.error_item(item, exc)
            pending = self.pending(item, "ROLLBACK" if rollback else "FORWARD")
            if pending and result["state"] == "UNAVAILABLE":
                result["state"] = "UNCERTAIN"
            self.record("STOPPED", item, direction="ROLLBACK" if rollback else "FORWARD",
                        **{k: v for k, v in result.items() if k != "source_id"})
            return result

    def run(self, *, apply=False, confirm_plan_sha256=None, limit=999, rollback=False,
            workers=1, transport_factory=None, source_ids=None):
        if not apply or not self.transport.apply_enabled or confirm_plan_sha256 != self.plan_sha256:
            raise SafetyError("EXPLICIT_APPLY_AND_EXACT_PLAN_HASH_REQUIRED")
        if type(workers) is not int or not 1 <= workers <= 32 or type(limit) is not int or limit < 1:
            raise SafetyError("BOUNDED_WORKERS_AND_LIMIT_REQUIRED")
        if workers > 1 and transport_factory is None:
            raise SafetyError("INDEPENDENT_WORKER_TRANSPORT_FACTORY_REQUIRED")
        selected = list(self.items)
        if source_ids is not None:
            source_ids = list(source_ids)
            if not source_ids or len(source_ids) != len(set(source_ids)) or not set(source_ids) <= set(self.by_id):
                raise SafetyError("UNIQUE_APPROVED_SOURCE_IDS_REQUIRED")
            selected = [i for i in selected if i["source_id"] in set(source_ids)]
        if len({i["source_id"] for i in selected}) != len(selected):
            raise SafetyError("DUPLICATE_CONCURRENT_SOURCE_ID")
        with self.journal.shared_locked(), self.authority_session():
            self.authority()
            backup = self.journal.root.parent / "backups"
            backup.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.last_backup_path = None
            self.last_backup_manifest = None
            self.snapshot_backup(backup / ("desired-before-" + str(uuid.uuid4())), "Backup of approved batch")
            self.journal.append("DESIRED_POLICY_BOUND", payload={"plan_sha256": self.plan_sha256,
                "run_id": self.plan.run_id, "policy": "exact-desired-state-v1", "direction": "ROLLBACK" if rollback else "FORWARD",
                "dependencies": {i["source_id"]: i["dependencies"] for i in selected}, "global_phases_removed": True,
                "audit_permissions": self.audit_permissions, "workers": workers,
                "selected_source_ids": [i["source_id"] for i in selected], "maximum_new_changes": limit})
            output, satisfied, changed = [], set(), 0
            selected_ids = {i["source_id"] for i in selected}
            dependencies = {i["source_id"]: set(i["dependencies"]) for i in selected}
            if rollback:
                selected = [i for i in reversed(selected) if self.latest_change(i)]
                dependencies = {i["source_id"]: {other["source_id"] for other in self.items
                    if i["source_id"] in other["dependencies"] and self.latest_change(other)} for i in selected}
            local, factory_gate, clients = threading.local(), threading.Lock(), []
            def worker(item):
                if not hasattr(local, "runner"):
                    transport = transport_factory() if transport_factory else self.transport
                    with factory_gate:
                        if workers > 1 and (transport is self.transport or any(transport is old for old in clients)):
                            raise SafetyError("NON_INDEPENDENT_WORKER_TRANSPORT")
                        clients.append(transport)
                    local.runner = copy.copy(self)
                    local.runner.transport = transport
                    local.runner.report_progress = self.report_progress
                return local.runner.execute_item(item, rollback=rollback)
            def finished(item, result):
                nonlocal changed
                output.append(result)
                if result["state"] in ({"ROLLED_BACK", "NOTHING_TO_ROLL_BACK"} if rollback else {"APPLIED", "TARGET"}):
                    satisfied.add(item["source_id"])
                if result["state"] in {"APPLIED", "ROLLED_BACK"}:
                    changed += 1
                self.report_progress("item_finished", completed_items=len(output), total_items=len(selected), **result)
            try:
                # Recover remote success before walking parent dependencies.
                # This records a fresh observation only; it never sends a write
                # or retries an unresolved child behind an unsatisfied parent.
                if not rollback:
                    for item in selected:
                        attempt = self.pending(item, "FORWARD")
                        if not attempt:
                            continue
                        related_folders = [folder for folder in self.items
                            if folder["original"]["mime_type"] == FOLDER and folder["source_id"] != item["source_id"]
                            and (folder["source_id"] in item["original"]["parents"] + item["target"]["parents"]
                                 or item["source_id"] in {m["id"] for m in (folder["baseline"].get("subtree") or {}).get("members", [])})]
                        if not related_folders:
                            continue
                        target = self.journal.read_evidence(attempt["payload"]["target_ref"])
                        observed = self.read(item["source_id"])
                        if core(observed) == target:
                            before = self.journal.read_evidence(attempt["payload"]["before_ref"])
                            self.unchanged(before, observed, moving=core(before)["parents"] != target["parents"])
                            self.applied(item, before, observed, attempt["payload"], None, recovered=True)
                # Subset runs observe outside dependencies; never add them to mutation scope.
                external = set().union(*dependencies.values()) - selected_ids if dependencies else set()
                if not rollback:
                    for source_id in sorted(external):
                        try:
                            _, _, state = self.observe(self.by_id[source_id], dependency=True)
                            if state == "TARGET":
                                satisfied.add(source_id)
                        except Exception:
                            pass  # Descendants receive an explicit blocked result below.
                pending = list(selected)
                in_flight = {}
                fatal = None
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="uqmc-drive") as pool:
                    while pending or in_flight:
                        # Reserve one possible new effect for every outstanding item.
                        capacity = min(workers - len(in_flight), limit - changed - len(in_flight))
                        if not self.stop_requested and capacity > 0:
                            occupied_names = {(pid, i["target"]["name"].casefold())
                                for i in in_flight.values() for pid in i["target"]["parents"]}
                            ready = []
                            for item in pending:
                                keys = {(pid, item["target"]["name"].casefold()) for pid in item["target"]["parents"]}
                                if dependencies[item["source_id"]] <= satisfied and not keys & occupied_names:
                                    ready.append(item)
                                    occupied_names.update(keys)
                                    if len(ready) >= capacity:
                                        break
                            for item in ready:
                                if self.stop_requested:
                                    break
                                pending.remove(item)
                                self.report_progress("item_started", source_id=item["source_id"],
                                    mode="ROLLBACK" if rollback else "APPLY", completed_items=len(output), total_items=len(selected))
                                in_flight[pool.submit(worker, item)] = item
                        if not in_flight:
                            if self.stop_requested or changed >= limit:
                                break
                            # Anything remaining is blocked by failed/out-of-scope dependencies.
                            for item in pending:
                                result = {"source_id": item["source_id"], "state": "DEPENDENCY_BLOCKED",
                                    "dependency_ids": sorted(dependencies[item["source_id"]] - satisfied)}
                                self.record("STOPPED", item, direction="ROLLBACK" if rollback else "FORWARD",
                                    state=result["state"], dependency_ids=result["dependency_ids"])
                                finished(item, result)
                            break
                        done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                        for future in done:
                            item = in_flight.pop(future)
                            try:
                                result = future.result()
                            except Exception as exc:
                                result = self.error_item(item, exc)
                                self.record("STOPPED", item, direction="ROLLBACK" if rollback else "FORWARD",
                                    **{k:v for k,v in result.items() if k != "source_id"})
                            except BaseException as exc:
                                # Simulated/process-level interruption: join already dispatched
                                # work and preserve unresolved intents for restart reconciliation.
                                self.stop_requested = True
                                fatal = fatal or exc
                                continue
                            finished(item, result)
                    if fatal:
                        raise fatal
                return self.summary(output, rollback=rollback, workers=workers,
                    selected_items=len(selected), new_changes=changed)
            finally:
                final_backup = backup / ("desired-after-" + str(uuid.uuid4()))
                manifest = self.snapshot_backup(final_backup, "Final recovery backup")
                self.last_backup_manifest = manifest
                self.last_backup_path = final_backup

    def summary(self, items, **extra):
        return {"schema_version": "uqmc.desired.status.v1", "plan_sha256": self.plan_sha256,
            "generated_at_utc": utcnow(), "journal_sequence": len(self.journal.records()),
            "coverage": "items_observed_this_invocation", "total_plan_items": len(self.items),
            "processed_items": len(items), "unprocessed_items": len(self.items) - len(items),
            "counts": dict(Counter(i["state"] for i in items)), "items": items, "paused": self.stop_requested,
            "progress_output_failed": self.progress_output_failed, "audit_permissions": self.audit_permissions, **extra}


def export_status(journal, summary, directory):
    """Private bulk output only. This is not the legacy strict snapshot schema."""
    target = Path(directory)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = str(uuid.uuid4())
    status_path = save_new(target / (token + "-desired-status.json"), summary)
    # An event-level transport uses the existing 26 Operation Log columns;
    # consumers append by Event ID in bounded batches, never per Drive action.
    rows = []
    states = {"DESIRED_PREPARED": "PREPARED", "DESIRED_ATTEMPTED": "ATTEMPTED", "DESIRED_APPLIED": "VERIFIED_APPLIED", "DESIRED_ROLLED_BACK": "VERIFIED_APPLIED", "DESIRED_STOPPED": "BLOCKED_CONFLICT"}
    for event in journal.records():
        if event["kind"] not in states or event["payload"].get("plan_sha256") != summary["plan_sha256"]:
            continue
        p = event["payload"]
        if not p.get("before_ref") or not p.get("target_ref"):
            continue
        before, intended = (journal.read_evidence(p[k]) for k in ("before_ref", "target_ref"))
        observed = journal.read_evidence(p["observed_ref"]) if p.get("observed_ref") else None
        args = p.get("args", {})
        action = "MOVE_AND_RENAME" if "name" in args and ("addParents" in args or "removeParents" in args) else "RENAME" if "name" in args else "MOVE"
        rows.append([event["event_id"], event["at"], p["run_id"], event["operation_id"], event["attempt"], p["operation_order"], json.dumps(p.get("dependencies", [])), p["direction"], action,
            json.dumps(p.get("original_operation_ids", [])), p["source_id"], core(before)["mime_type"], before["name"], json.dumps(before["parents"]), intended["name"], json.dumps(intended["parents"]), "UNCERTAIN" if p.get("state") == "UNCERTAIN" else states[event["kind"]],
            observed["name"] if observed else None, json.dumps(observed["parents"]) if observed else None, p["before_ref"], p.get("observed_ref"), p["before_ref"], p.get("forward_event_id"), "explicit local CLI", p.get("request_outcome"), event["hash"]])
    log_path = save_new(target / (token + "-operation-log.json"), {"headers": HEADERS, "rows": rows, "idempotency_column": "Event ID", "private": True})
    return {"status": status_path, "operation_log": log_path, "published": False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("preview", "apply", "resume", "rollback"))
    p.add_argument("--store", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--expect-account", help="Optional exact Google email to verify before any item is processed")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--confirm-plan-sha256")
    p.add_argument("--limit", type=int, default=999)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--workers", type=int, default=10, help="Independent mutation workers, 1..32 (default: 10)")
    p.add_argument("--source-ids-file", help="Private JSON array selecting an exact approved subset; no added dependencies")
    p.add_argument("--export-dir")
    p.add_argument("--skip-permission-audit", action="store_true",
        help="Skip permission reads/comparisons only with existing accepted section exposure; never edits sharing")
    args = p.parse_args(argv)
    if args.limit < 1 or not 1 <= args.workers <= 32:
        p.error("--limit must be positive and --workers must be 1..32")
    journal = Journal(args.store)
    plan = plan_from_json(json.loads(Path(args.plan).read_text()))
    mutating = args.command != "preview"
    summary = None
    try:
        runner = DesiredRunner(journal, plan, None, max_attempts=args.max_attempts,
            progress=lambda event: print(json.dumps(event), flush=True),
            audit_permissions=not args.skip_permission_audit)
        plan = runner.plan
        if mutating and (not args.apply or args.confirm_plan_sha256 != runner.plan_sha256):
            raise SafetyError("EXPLICIT_APPLY_AND_EXACT_PLAN_HASH_REQUIRED")
        if not mutating and args.apply:
            raise SafetyError("PREVIEW_CANNOT_APPLY")
        if args.command in {"apply", "resume"}:
            from .batch_source import require_batch_source
            require_batch_source(journal, plan, resume=args.command == "resume")
        items = runner.items
        allowed = {i["source_id"] for i in items}
        for i in items:
            allowed.update(i["original"]["parents"] + i["target"]["parents"])
            allowed.update(m["id"] for m in (i["baseline"].get("subtree") or {}).get("members", []))
        # Previously reviewed access-baseline ancestors are the only additional
        # IDs admitted for folder cycle checks; no ambient Drive traversal.
        for r in journal.records():
            if r["kind"] == "HANDOFF_AUTHORIZED" and r["payload"].get("proposal_hash") == plan.proposal_hash:
                auth = journal.read_evidence(r["payload"]["authorization_ref"])
                if auth.get("exposure_review_ref"):
                    allowed.update(journal.read_evidence(auth["exposure_review_ref"]).get("access_baselines", {}))
        excluded = set(plan.excluded_ids) | {r["payload"]["file_id"] for r in journal.records() if r["kind"] == "EXCLUDED"}
        selected_ids = json.loads(Path(args.source_ids_file).read_text()) if args.source_ids_file else None
        if selected_ids is not None and (not isinstance(selected_ids, list) or not selected_ids or
                any(not isinstance(value, str) for value in selected_ids) or
                len(selected_ids) != len(set(selected_ids)) or not set(selected_ids) <= {i["source_id"] for i in items}):
            raise SafetyError("UNIQUE_APPROVED_SOURCE_IDS_REQUIRED")
        mutation_ids = set(selected_ids) if selected_ids is not None else {i["source_id"] for i in items}
        def make_transport():
            return GoogleDriveTransport.from_authorized_user_file(args.token, apply=mutating, allowed_ids=allowed,
                mutation_ids=mutation_ids, excluded_ids=excluded, audit_permissions=not args.skip_permission_audit)
        transport = make_transport()
        account = transport.get_account()
        if args.expect_account and account["emailAddress"].casefold() != args.expect_account.casefold():
            raise SafetyError("AUTHENTICATED_ACCOUNT_MISMATCH")
        print(json.dumps({"authenticated_account": account, "source_mutation_allowlist_count": len(items)}), flush=True)
        runner.transport = transport
        def worker_transport():
            client = make_transport()
            if client.get_account()["emailAddress"].casefold() != account["emailAddress"].casefold():
                raise SafetyError("WORKER_AUTHENTICATED_ACCOUNT_MISMATCH")
            return client
        if selected_ids is not None and not mutating:
            runner.items = [i for i in runner.items if i["source_id"] in mutation_ids]
        def stop(signum, frame):
            runner.stop_requested = True
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        summary = runner.run(apply=args.apply, confirm_plan_sha256=args.confirm_plan_sha256, limit=args.limit, rollback=args.command == "rollback",
            workers=args.workers, transport_factory=worker_transport, source_ids=selected_ids) if mutating else runner.preview(limit=args.limit)
        summary["authenticated_account"] = account
        print(json.dumps(summary))
        bad = {"CONFLICT", "UNAVAILABLE", "UNCERTAIN", "DEPENDENCY_BLOCKED"} | ({"NOT_APPLIED", "PARTIAL"} if mutating else set())
        return 0 if not any(i["state"] in bad for i in summary["items"]) else 2
    except Exception as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, SafetyError) else type(exc).__name__,
            **({"request_error": exc.as_dict()} if isinstance(exc, TransportError) else {})}), flush=True)
        return 2
    finally:
        if args.export_dir and summary:
            try:
                print(json.dumps({"exports": export_status(journal, summary, args.export_dir)}))
            except Exception as exc:
                print(json.dumps({"export_failed": type(exc).__name__, "actions_not_retried": True}))


if __name__ == "__main__":
    raise SystemExit(main())
