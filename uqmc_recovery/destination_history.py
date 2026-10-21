"""Read-only reconciliation of fresh destination proposals with durable history.

The caller reads Drive once, resolves logical destinations to exact parent IDs,
checks fresh section authority/exclusions, and freezes pending work. This module
never approves, dispatches, appends evidence, or substitutes for DesiredRunner's
strict pre-write/readback checks. A known current state is not proof that an old
request never ran; historical uncertainty is retained in every result.
"""
from __future__ import annotations

import copy

from .model import SafetyError, digest

_FIELDS = ("name", "parents")
_LEGACY = {"PREPARED", "ATTEMPTED", "UNCERTAIN", "VERIFIED_APPLIED",
           "RECONCILED_AT_TARGET", "FAILED_NOT_APPLIED", "BLOCKED_CONFLICT", "SKIPPED"}
_OBSERVATIONS = {"DESIRED_APPLIED", "DESIRED_ROLLED_BACK", "DESIRED_OBSERVED",
                 "DESIRED_REBASELINED", "DESIRED_FOLDER_RECONCILED"}
_PROTECTED = (("size", "size"), ("md5Checksum", "md5_checksum"),
              ("owners", "owners"), ("driveId", "drive_id"),
              ("shortcutDetails", "shortcut_details"))


def _core(raw):
    mime = raw.get("mimeType", raw.get("mime_type"))
    if (not raw.get("id") or not isinstance(raw.get("name"), str)
            or not isinstance(raw.get("parents"), list) or not mime
            or any(not isinstance(p, str) or not p for p in raw["parents"])
            or len(set(raw["parents"])) != len(raw["parents"])):
        raise SafetyError("DESTINATION_HISTORY_INCOMPLETE_STATE")
    return {"id": raw["id"], "name": raw["name"],
            "parents": sorted(raw["parents"]), "mime_type": mime}


def _ordered(operations):
    result, pending = [], list(operations)
    if len({o["operation_id"] for o in pending}) != len(pending):
        raise SafetyError("DESTINATION_HISTORY_DUPLICATE_OPERATION")
    while pending:
        ready = [o for o in pending if set(o.get("depends_on", [])) <= {o["operation_id"] for o in result}]
        if not ready:
            raise SafetyError("DESTINATION_HISTORY_INVALID_DEPENDENCIES")
        result.extend(ready)
        pending = [o for o in pending if o not in ready]
    return result


def build_history_index(journal):
    """Return stable-ID histories from verified frozen plans and evidence.

    Uses one records() read and caches each referenced evidence document. The
    journal implementation must validate its hash chain and evidence digests.
    Frozen plan content is additionally checked against proposal_hash here.
    Rollback is retained as an explicit state, never implicitly re-applied by a
    fresh destination command. No real or fixture journal is modified.
    """
    records, evidence, plans, histories = journal.records(), {}, {}, {}

    def read(ref):
        if not ref:
            raise SafetyError("DESTINATION_HISTORY_EVIDENCE_MISSING")
        if ref not in evidence:
            evidence[ref] = journal.read_evidence(ref)
        return copy.deepcopy(evidence[ref])

    for event in records:
        if event["kind"] != "DRAFTED":
            continue
        p = event["payload"]
        body = read(p.get("plan_ref"))
        plan_hash = p.get("proposal_hash")
        if not plan_hash or digest(body) != plan_hash:
            raise SafetyError("DESTINATION_HISTORY_PLAN_HASH_MISMATCH")
        groups, operations = {}, {}
        for op in _ordered(body["operations"]):
            if op["action"] not in {"MOVE", "RENAME"}:
                continue
            source = op["file_id"]
            before = _core(op["before"])
            if before["id"] != source:
                raise SafetyError("DESTINATION_HISTORY_ID_MISMATCH")
            group = groups.setdefault(source, {"original": before, "target": copy.deepcopy(before)})
            group["target"].update(copy.deepcopy(op["target"]))
            group["target"] = _core(group["target"])
            operations[op["operation_id"]] = op
            h = histories.setdefault(source, {"source_id": source, "original": before,
                "baseline": copy.deepcopy(op["before"]), "states": [before],
                "completed_fields": {}, "rolled_back_fields": [], "latest_observation": None,
                "latest_observation_ref": None, "last_applied": None, "plan_hashes": [], "pending_requests": [],
                "historical_uncertain_operation_ids": [], "event_ids": []})
            if plan_hash not in h["plan_hashes"]:
                h["plan_hashes"].append(plan_hash)
        plans[plan_hash] = {"groups": groups, "operations": operations}

    pending, forward_events = {}, {}
    for event in records:
        kind, p = event["kind"], event["payload"]
        if kind not in _LEGACY | _OBSERVATIONS | {"DESIRED_ATTEMPTED", "DESIRED_RECEIPT"}:
            continue
        plan_hash = p.get("plan_sha256", p.get("proposal_hash"))
        source = p.get("source_id", p.get("file_id"))
        if not source:
            continue
        plan = plans.get(plan_hash)
        if not plan or source not in plan["groups"]:
            raise SafetyError("DESTINATION_HISTORY_EVENT_NOT_IN_PLAN")
        h, group = histories[source], plan["groups"][source]
        op = plan["operations"].get(event.get("operation_id"))
        if kind in _LEGACY and (not op or op["file_id"] != source):
            raise SafetyError("DESTINATION_HISTORY_OPERATION_MISMATCH")
        h["event_ids"].append(event["event_id"])
        key = (plan_hash, event.get("operation_id"), p.get("direction", "FORWARD"))
        if (kind == "UNCERTAIN"
                or p.get("request_outcome") in {"UNKNOWN", "UNKNOWN_RECOVERED_BY_READ"}
                or kind == "RECONCILED_AT_TARGET"):
            if event["operation_id"] not in h["historical_uncertain_operation_ids"]:
                h["historical_uncertain_operation_ids"].append(event["operation_id"])
        if kind in {"ATTEMPTED", "UNCERTAIN", "DESIRED_ATTEMPTED"}:
            before = _core(read(p.get("before_ref"))) if p.get("before_ref") else _core(op["before"])
            target_ref = p.get("target_ref", p.get("intended_ref"))
            target = _core(read(target_ref)) if target_ref else _core({**before, **op["target"]})
            if before["id"] != source or target["id"] != source:
                raise SafetyError("DESTINATION_HISTORY_ID_MISMATCH")
            pending[key] = {"event_id": event["event_id"], "plan_sha256": plan_hash,
                            "operation_id": event["operation_id"], "direction": p.get("direction", "FORWARD"),
                            "before": before, "target": target}
            for state in (before, target):
                if state not in h["states"]:
                    h["states"].append(state)
        verified = kind in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET", "DESIRED_APPLIED", "DESIRED_ROLLED_BACK"}
        observed = kind in _OBSERVATIONS and (kind != "DESIRED_OBSERVED" or p.get("state") in {"TARGET", "PARTIAL", "NOT_APPLIED"})
        if not (verified or observed):
            continue
        ref = p.get("observed_ref", p.get("applied_ref"))
        raw = read(ref)
        current = _core(raw)
        if current["id"] != source or current["mime_type"] != h["original"]["mime_type"]:
            raise SafetyError("DESTINATION_HISTORY_ID_MISMATCH")
        h.update(latest_observation=current, latest_observation_ref=ref, baseline=raw)
        if current not in h["states"]:
            h["states"].append(current)
        if verified:
            pending.pop(key, None)
            # A later verified result can reconcile an older adapter's request
            # for this identity. Retain the historical uncertainty; never replay
            # it merely because the operation IDs/plan hashes changed.
            for pending_key, request in list(pending.items()):
                changed = [f for f in _FIELDS if request["before"][f] != request["target"][f]]
                if (request["before"]["id"] == source and
                        all(current[f] == request["target"][f] for f in changed)):
                    if request["operation_id"] not in h["historical_uncertain_operation_ids"]:
                        h["historical_uncertain_operation_ids"].append(request["operation_id"])
                    del pending[pending_key]
        rollback = p.get("direction") == "ROLLBACK" or kind == "DESIRED_ROLLED_BACK"
        if rollback:
            if kind == "DESIRED_ROLLED_BACK":
                forward = forward_events.get(p.get("forward_event_id"))
                if not forward or forward["source_id"] != source:
                    raise SafetyError("DESTINATION_HISTORY_ROLLBACK_LINK_MISSING")
                before, after = forward["before"], forward["after"]
                fields = [f for f in _FIELDS if before[f] != after[f]]
                if any(current[f] != before[f] for f in fields):
                    raise SafetyError("DESTINATION_HISTORY_ROLLBACK_PREIMAGE_MISMATCH")
            else:
                fields = list(op["target"])
                if any(current[f] != op["target"][f] for f in fields):
                    raise SafetyError("DESTINATION_HISTORY_ROLLBACK_PREIMAGE_MISMATCH")
            for field in fields:
                h["completed_fields"].pop(field, None)
                if field not in h["rolled_back_fields"]:
                    h["rolled_back_fields"].append(field)
        elif verified or kind == "DESIRED_OBSERVED":
            if kind == "DESIRED_APPLIED":
                applied_before = _core(read(p.get("before_ref")))
                applied_target = _core(read(p.get("target_ref")))
                if current != applied_target or applied_before["id"] != source:
                    raise SafetyError("DESTINATION_HISTORY_VERIFIED_TARGET_MISMATCH")
                fields = [f for f in _FIELDS if applied_before[f] != applied_target[f]]
            else:
                fields = list(op["target"]) if kind in _LEGACY else [f for f in _FIELDS if group["original"][f] != group["target"][f]]
            for field in fields:
                target = op["target"][field] if kind in _LEGACY else group["target"][field]
                if current[field] == target:
                    h["completed_fields"][field] = copy.deepcopy(target)
                    if field in h["rolled_back_fields"]:
                        h["rolled_back_fields"].remove(field)
                elif verified:
                    raise SafetyError("DESTINATION_HISTORY_VERIFIED_TARGET_MISMATCH")
        if kind == "DESIRED_APPLIED":
            before = _core(read(p.get("before_ref")))
            if before["id"] != source:
                raise SafetyError("DESTINATION_HISTORY_ID_MISMATCH")
            forward_events[event["event_id"]] = {"source_id": source, "before": before, "after": current}
            h["last_applied"] = {"event_id": event["event_id"], "plan_sha256": plan_hash,
                "before": before, "after": current, "before_ref": p["before_ref"],
                "applied_ref": p.get("applied_ref", ref), "target_ref": p["target_ref"]}
    for request in pending.values():
        h = histories[request["before"]["id"]]
        h["pending_requests"].append(request)
        if request["operation_id"] not in h["historical_uncertain_operation_ids"]:
            h["historical_uncertain_operation_ids"].append(request["operation_id"])
    return histories


def reconcile_item(history, desired, live, *, baseline=None, approved=True,
                   excluded=False, logical_destination=None, prior_logical_destination=None):
    """Classify a fresh desired core against history and an exact live read.

    READY contains only still-needed effects. COMPLETE means current read matches
    desired, not an attribution claim. RECOVER_AT_TARGET requires DesiredRunner
    to record the pending request's readback before finishing. All pending
    requests remain exposed, including known-original observations. The caller
    must resume their existing plan before substituting a changed request target.
    """
    desired, current = _core(desired), _core(live)
    h = history or {}
    original = _core(h.get("original") or baseline or live)
    completed = h.get("completed_fields", {})
    requests = copy.deepcopy(h.get("pending_requests", []))
    result = {"source_id": desired["id"], "disposition": "READY", "reason": None,
              "current": current, "desired": desired, "pending_fields": [],
              "completed_fields": copy.deepcopy(completed), "pending_requests": requests,
              "historical_request_uncertainty": bool(h.get("historical_uncertain_operation_ids")),
              "historical_uncertain_operation_ids": list(h.get("historical_uncertain_operation_ids", [])),
              "recovery_required": bool(requests), "previously_verified": bool(h.get("latest_observation"))}

    def blocked(reason):
        return result | {"disposition": "BLOCKED", "reason": reason}

    if excluded or not approved:
        return blocked("SOURCE_EXCLUDED" if excluded else "FRESH_SECTION_AUTHORITY_REMOVED")
    if (current["id"] != desired["id"] or original["id"] != desired["id"] or
            current["mime_type"] != desired["mime_type"] or original["mime_type"] != current["mime_type"]):
        return blocked("IDENTITY_OR_MIME_DRIFT")
    if live.get("trashed"):
        return blocked("SOURCE_TRASHED")
    known = h.get("baseline") or baseline or {}
    for raw_key, normalized_key in _PROTECTED:
        value = known.get(raw_key, known.get(normalized_key))
        actual = live.get(raw_key, live.get(normalized_key))
        if value is not None and value != actual:
            return blocked("PROTECTED_METADATA_DRIFT:" + raw_key)
    if ("parents" in completed and prior_logical_destination is not None
            and logical_destination != prior_logical_destination):
        return blocked("COMPLETED_DESTINATION_CHANGED_REQUIRES_SEPARATE_WORK")
    for field, value in completed.items():
        if desired[field] != value:
            return blocked("COMPLETED_EFFECT_CHANGED_REQUIRES_SEPARATE_WORK:" + field)
        if current[field] != value:
            return blocked("COMPLETED_EFFECT_DRIFT:" + field)
    if any(current[f] != desired[f] for f in h.get("rolled_back_fields", [])):
        return blocked("ROLLED_BACK_EFFECT_REQUIRES_EXPLICIT_RESUME")
    states = h.get("states") or [original]
    for field in _FIELDS:
        if current[field] != desired[field] and not any(current[field] == state[field] for state in states):
            return blocked("UNAPPROVED_CURRENT_STATE:" + field)
    for request in requests:
        if request["direction"] == "ROLLBACK":
            return blocked("PENDING_ROLLBACK_REQUIRES_EXPLICIT_RESUME")
        changed = [f for f in _FIELDS if request["before"][f] != request["target"][f]]
        if any(desired[f] != request["target"][f] for f in changed):
            return blocked("PENDING_REQUEST_TARGET_CHANGED_RECONCILE_EXISTING_PLAN")
    result["pending_fields"] = [f for f in _FIELDS if current[f] != desired[f]]
    if not result["pending_fields"]:
        result["disposition"] = "RECOVER_AT_TARGET" if requests else "COMPLETE"
    return result
