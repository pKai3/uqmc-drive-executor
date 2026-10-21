"""Standalone desired-state runner over an existing approved frozen plan.

No assistant action tickets, global rename phase, automatic rollback or OAuth
flow. Historical request uncertainty and observed current state are distinct.
"""
from __future__ import annotations

import argparse
import copy
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import time
import uuid

from .__main__ import plan_from_json, save_new
from .direct_transport import GoogleDriveTransport, TransportError
from .journal import Journal
from .model import FOLDER, SafetyError, digest, validate_extension_evidence, validate_rename_name
from .projection import HEADERS
from .section_importer import validate_section_authority


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


def compile_items(plan, journal):
    """Keep exact approved effects; replace phase ordering with target-folder edges."""
    records = journal.records()
    frozen = [r for r in records if r["kind"] == "DRAFTED" and r["payload"].get("proposal_hash") == plan.proposal_hash]
    if not frozen or digest(journal.read_evidence(frozen[-1]["payload"]["plan_ref"])) != plan.proposal_hash:
        raise SafetyError("EXISTING_FROZEN_PLAN_REQUIRED")
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
    def __init__(self, journal, plan, transport, *, max_attempts=3, retry_delay=1, fault=None, progress=None):
        if not 1 <= max_attempts <= 5:
            raise SafetyError("MAX_ATTEMPTS_MUST_BE_1_TO_5")
        self.journal, self.plan, self.transport = journal, plan, transport
        self.max_attempts, self.retry_delay = max_attempts, retry_delay
        self.fault = fault or (lambda *args: None)
        self.stop_requested = False
        self.progress = progress or (lambda event: None)
        self.progress_output_failed = False
        self.items = compile_items(plan, journal)
        self.by_id = {i["source_id"]: i for i in self.items}

    def report_progress(self, event, **value):
        try:
            self.progress({"event": event, **value})
        except Exception:
            # An output failure must not turn a verified action into a retry.
            self.progress_output_failed = True

    def events(self, source_id=None):
        return [r for r in self.journal.records() if r["kind"].startswith("DESIRED_")
                and r["payload"].get("plan_sha256") == self.plan.proposal_hash
                and (source_id is None or r["payload"].get("source_id") == source_id)]

    def record(self, kind, item, *, direction="FORWARD", attempt=0, **payload):
        return self.journal.append("DESIRED_" + kind,
            operation_id="desired:" + self.plan.proposal_hash[:12] + ":" + item["source_id"] + ":" + direction,
            attempt=attempt, payload={"plan_sha256": self.plan.proposal_hash, "run_id": self.plan.run_id,
                "source_id": item["source_id"], "direction": direction,
                "operation_order": item["operation_order"], "dependencies": item["dependencies"],
                "original_operation_ids": [o.operation_id for o in item["operations"]],
                "historical_uncertain_operation_ids": item["historical_uncertain_operation_ids"], **payload})

    def read(self, source_id):
        raw = self.transport.get_file(source_id)
        if raw.get("id") != source_id or raw.get("trashed") is True:
            raise SafetyError("SOURCE_ID_OR_TRASH_CONFLICT")
        core(raw)
        raw["_permissions"] = sorted(self.transport.list_permissions(source_id), key=digest)
        return raw

    def authority(self):
        validate_section_authority(self.journal, self.plan)
        auths = [r for r in self.journal.records() if r["kind"] == "HANDOFF_AUTHORIZED"
                 and r["payload"].get("proposal_hash") == self.plan.proposal_hash]
        if not auths:
            raise SafetyError("EXISTING_SECTION_AUTHORIZATION_REQUIRED")
        auth = self.journal.read_evidence(auths[-1]["payload"]["authorization_ref"])
        if auth.get("authorized") is not True or auth.get("proposal_hash") != self.plan.proposal_hash:
            raise SafetyError("AUTHORIZATION_MISMATCH")
        return auth

    def latest_change(self, item):
        changes = [r for r in self.events(item["source_id"]) if r["kind"] == "DESIRED_APPLIED"]
        rolled = {r["payload"].get("forward_event_id") for r in self.events(item["source_id"]) if r["kind"] == "DESIRED_ROLLED_BACK"}
        return next((r for r in reversed(changes) if r["event_id"] not in rolled), None)

    def approved(self, item, raw):
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
            applied = self.journal.read_evidence(active["payload"]["applied_ref"])
            if core(raw) != core(applied):
                raise SafetyError("RECORDED_APPLIED_STATE_WAS_CHANGED")
            self.unchanged(applied, raw, strict=True)
        else:
            for old, native in IMMUTABLE.items():
                if baseline.get(old) is not None and raw.get(native) != baseline[old]:
                    raise SafetyError("KNOWN_CONTENT_OR_OWNER_DRIFT")
            # At original parents, every historically known permission field
            # remains mandatory. Additional native fields stay in raw evidence.
            if current["parents"] == item["original"]["parents"] and baseline.get("permissions") is not None:
                old = baseline["permissions"]
                keys = set().union(*(set(p) for p in old)) if old else set()
                projected = [{k: p.get(k) for k in keys} for p in raw["_permissions"]]
                if sorted(old, key=digest) != sorted(projected, key=digest):
                    raise SafetyError("KNOWN_PERMISSION_DRIFT")
        return current

    @staticmethod
    def unchanged(before, after, *, strict=False, moving=False):
        for key in ("id", "mimeType", "size", "md5Checksum", "owners", "driveId", "shortcutDetails", "trashed"):
            if key in before and before[key] != after.get(key):
                raise SafetyError("PROTECTED_METADATA_DRIFT")
        if not moving and before.get("_permissions") != after.get("_permissions"):
            raise SafetyError("PERMISSION_DRIFT")
        if moving:
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

    def preflight(self, item, before, target, *, rollback=False):
        parents = {}
        if before["name"] != target["name"] and before.get("capabilities", {}).get("canRename") is False:
            raise SafetyError("SOURCE_CANNOT_RENAME")
        for pid in sorted(set(before["parents"]) | set(target["parents"])):
            parent = self.read(pid)
            if parent.get("mimeType") != FOLDER:
                raise SafetyError("DESTINATION_NOT_FOLDER")
            if pid in self.by_id:
                self.approved(self.by_id[pid], parent)
            parents[pid] = parent
        for pid in target["parents"]:
            if pid not in before["parents"]:
                destination = parents[pid]
                if destination.get("capabilities", {}).get("canAddChildren") is False:
                    raise SafetyError("DESTINATION_CANNOT_ADD_CHILDREN")
                capability = "canMoveItemWithinDrive" if before.get("driveId") == destination.get("driveId") else "canMoveItemOutOfDrive"
                if before.get("capabilities", {}).get(capability) is False:
                    raise SafetyError("SOURCE_CANNOT_MOVE_" + capability)
            siblings = self.transport.list_children(pid)
            collision = {s["id"] for s in siblings if s["id"] != item["source_id"] and s["name"].casefold() == target["name"].casefold()}
            if collision - item["allowed_collision_ids"]:
                raise SafetyError("UNAPPROVED_SIBLING_COLLISION")
        if core(before)["parents"] != target["parents"]:
            if not rollback and self.authority().get("access_policy") != "SCOPED_EXPOSURE_ACCEPTED":
                raise SafetyError("MOVE_ACCESS_REVIEW_REQUIRED")
            if before.get("mimeType") == FOLDER:
                subtree = item["baseline"].get("subtree") or {}
                if not subtree.get("complete"):
                    raise SafetyError("COMPLETE_FOLDER_MEMBERSHIP_REQUIRED")
                pending, seen, members = [item["source_id"]], set(), []
                while pending:
                    pid = pending.pop()
                    if pid in seen:
                        raise SafetyError("FOLDER_CYCLE")
                    seen.add(pid)
                    children = self.transport.list_children(pid)
                    members.extend(children)
                    pending.extend(c["id"] for c in children if c["mimeType"] == FOLDER)
                if set(target["parents"]) & ({item["source_id"]} | {m["id"] for m in members}):
                    raise SafetyError("FOLDER_CYCLE")
                original = {m["id"]: m for m in (item["baseline"].get("subtree") or {}).get("members", [])}
                permitted = set(original) | {i["source_id"] for i in self.items if item["source_id"] in i["target"]["parents"]}
                if any(m["id"] not in permitted for m in members) or not set(original) <= {m["id"] for m in members}:
                    raise SafetyError("FOLDER_MEMBERSHIP_DRIFT")
                for member in members:
                    known = self.by_id.get(member["id"])
                    if known and (member["name"] not in (known["original"]["name"], known["target"]["name"]) or member["mimeType"] != known["original"]["mime_type"]):
                        raise SafetyError("FOLDER_MEMBER_METADATA_DRIFT")
        return parents

    def observe(self, item):
        raw = self.read(item["source_id"])
        current = self.approved(item, raw)
        partial = any(current[f] == item["target"][f] != item["original"][f] for f in ("name", "parents"))
        return raw, current, "TARGET" if current == item["target"] else "PARTIAL" if partial else "NOT_APPLIED"

    def preview(self, *, limit=None):
        with self.journal.locked():
            self.authority()
            output = []
            for item in self.items:
                if self.stop_requested or (limit is not None and len(output) >= limit):
                    break
                self.report_progress("item_started", source_id=item["source_id"], mode="PREVIEW",
                    completed_items=len(output), total_items=len(self.items))
                try:
                    raw, current, state = self.observe(item)
                    parents = self.preflight(item, raw, item["target"])
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
                    request_outcome="UNKNOWN", error_type=type(exc).__name__, http_status=getattr(exc, "status", None), **payload)
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
            transient = isinstance(error, (TimeoutError, ConnectionError)) or getattr(error, "transient", False) or getattr(error, "status", None) in {408, 429, 500, 502, 503, 504}
            if not transient or attempt == self.max_attempts:
                return {"source_id": item["source_id"], "state": "NOT_APPLIED", "reason": "RETRY_BUDGET_EXHAUSTED" if transient else "REQUEST_REFUSED_OR_UNEXPECTED_READBACK"}
            time.sleep(min(self.retry_delay * attempt, 5))
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

    def run(self, *, apply=False, confirm_plan_sha256=None, limit=999, rollback=False):
        if not apply or not self.transport.apply_enabled or confirm_plan_sha256 != self.plan.proposal_hash:
            raise SafetyError("EXPLICIT_APPLY_AND_EXACT_PLAN_HASH_REQUIRED")
        with self.journal.locked():
            self.authority()
            backup = self.journal.root.parent / "backups"
            backup.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.journal.backup(backup / ("desired-before-" + str(uuid.uuid4())))
            self.journal.append("DESIRED_POLICY_BOUND", payload={"plan_sha256": self.plan.proposal_hash,
                "run_id": self.plan.run_id, "policy": "exact-desired-state-v1", "direction": "ROLLBACK" if rollback else "FORWARD",
                "dependencies": {i["source_id"]: i["dependencies"] for i in self.items}, "global_phases_removed": True})
            output, satisfied, changed = [], set(), 0
            try:
                for item in reversed(self.items) if rollback else self.items:
                    if self.stop_requested or changed >= limit:
                        break
                    self.report_progress("item_started", source_id=item["source_id"], mode="ROLLBACK" if rollback else "APPLY",
                        completed_items=len(output), total_items=len(self.items))
                    try:
                        if rollback:
                            event = self.latest_change(item)
                            if not event:
                                continue
                            if any(item["source_id"] in other["dependencies"] and self.latest_change(other) for other in self.items):
                                result = {"source_id": item["source_id"], "state": "DEPENDENCY_BLOCKED"}
                            else:
                                raw = self.read(item["source_id"])
                                applied = self.journal.read_evidence(event["payload"]["applied_ref"])
                                target = core(self.journal.read_evidence(event["payload"]["before_ref"]))
                                pending = self.pending(item, "ROLLBACK")
                                if pending and core(raw) == target:
                                    result = self.transition(item, raw, target, rollback_event=event)
                                else:
                                    if core(raw) != core(applied):
                                        raise SafetyError("ROLLBACK_CURRENT_STATE_CONFLICT")
                                    self.unchanged(applied, raw, strict=True)
                                    result = self.transition(item, raw, target, rollback_event=event)
                        elif not set(item["dependencies"]) <= satisfied:
                            result = {"source_id": item["source_id"], "state": "DEPENDENCY_BLOCKED"}
                        else:
                            raw, current, state = self.observe(item)
                            if state == "TARGET" and self.pending(item, "FORWARD"):
                                result = self.transition(item, raw, item["target"])
                            elif state == "TARGET":
                                self.record("OBSERVED", item, observed_ref=self.journal.evidence(raw), state="TARGET")
                                result = {"source_id": item["source_id"], "state": "TARGET", "observed": current, "effects": self.effects(item, current)}
                            else:
                                result = self.transition(item, raw, item["target"])
                            if result["state"] in {"APPLIED", "TARGET"}:
                                satisfied.add(item["source_id"])
                        if result["state"] in {"APPLIED", "ROLLED_BACK"}:
                            changed += 1
                    except Exception as exc:
                        result = self.error_item(item, exc)
                        self.record("STOPPED", item, direction="ROLLBACK" if rollback else "FORWARD", **{k:v for k,v in result.items() if k != "source_id"})
                    output.append(result)
                    self.report_progress("item_finished", completed_items=len(output), total_items=len(self.items), **result)
                return self.summary(output, rollback=rollback)
            finally:
                self.journal.backup(backup / ("desired-after-" + str(uuid.uuid4())))

    def summary(self, items, **extra):
        return {"schema_version": "uqmc.desired.status.v1", "plan_sha256": self.plan.proposal_hash,
            "generated_at_utc": utcnow(), "journal_sequence": len(self.journal.records()),
            "coverage": "items_observed_this_invocation", "total_plan_items": len(self.items),
            "processed_items": len(items), "unprocessed_items": len(self.items) - len(items),
            "counts": dict(Counter(i["state"] for i in items)), "items": items, "paused": self.stop_requested,
            "progress_output_failed": self.progress_output_failed, **extra}


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
            json.dumps(p.get("original_operation_ids", [])), p["source_id"], core(before)["mime_type"], before["name"], json.dumps(before["parents"]), intended["name"], json.dumps(intended["parents"]), states[event["kind"]],
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
    p.add_argument("--export-dir")
    args = p.parse_args(argv)
    if args.limit < 1:
        p.error("--limit must be positive")
    journal = Journal(args.store)
    plan = plan_from_json(json.loads(Path(args.plan).read_text()))
    mutating = args.command != "preview"
    summary = None
    try:
        if mutating and (not args.apply or args.confirm_plan_sha256 != plan.proposal_hash):
            raise SafetyError("EXPLICIT_APPLY_AND_EXACT_PLAN_HASH_REQUIRED")
        if not mutating and args.apply:
            raise SafetyError("PREVIEW_CANNOT_APPLY")
        items = compile_items(plan, journal)
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
        transport = GoogleDriveTransport.from_authorized_user_file(args.token, apply=mutating, allowed_ids=allowed,
            mutation_ids={i["source_id"] for i in items}, excluded_ids=excluded)
        account = transport.get_account()
        if args.expect_account and account["emailAddress"].casefold() != args.expect_account.casefold():
            raise SafetyError("AUTHENTICATED_ACCOUNT_MISMATCH")
        print(json.dumps({"authenticated_account": account, "source_mutation_allowlist_count": len(items)}), flush=True)
        runner = DesiredRunner(journal, plan, transport, max_attempts=args.max_attempts,
            progress=lambda event: print(json.dumps(event), flush=True))
        def stop(signum, frame):
            runner.stop_requested = True
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        summary = runner.run(apply=args.apply, confirm_plan_sha256=args.confirm_plan_sha256, limit=args.limit, rollback=args.command == "rollback") if mutating else runner.preview(limit=args.limit)
        summary["authenticated_account"] = account
        print(json.dumps(summary))
        bad = {"CONFLICT", "UNAVAILABLE", "DEPENDENCY_BLOCKED"} | ({"NOT_APPLIED", "PARTIAL"} if mutating else set())
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
