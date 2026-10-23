"""Honest partial metadata for the deliberately agent-assisted review path.

This profile does not establish complete access/capability evidence, execution
authority, a version precondition, or permission to mutate Drive. It only keeps
exact observed identity/name/parents usable without inventing unavailable data.
"""
from __future__ import annotations

import copy
import re
from .model import (FOLDER, Operation, Plan, SafetyError, WITHDRAWN_ID, digest,
                    validate_rename_name, validate_extension_evidence, validate_rollback_links)

PROFILE = "AGENT_ASSISTED"
_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_OPTIONAL = ("version", "modified_time", "trashed", "permissions", "owners", "drive_id", "capabilities", "access")
_INFORMATIONAL = {"observation_profile", "metadata_unavailable", "observed_at", "observed_at_utc",
                  "metadata_verified_at_utc", "version", "modified_time", "missing_fields", "unavailable_fields"}


def _stable_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _ids(values, reason, *, empty=True):
    if not isinstance(values, (list, tuple)) or (not empty and not values):
        raise SafetyError(reason)
    if any(not _stable_id(value) for value in values) or len(values) != len(set(values)):
        raise SafetyError(reason)
    return set(values)


def _members(value):
    subtree = value.get("subtree")
    return subtree.get("members", []) if isinstance(subtree, dict) else []


def agent_snapshot(value):
    """Strict mutation snapshot: exact own parents are always required."""
    return _validated_snapshot(value)


def agent_parent_context(value, *, require_subtree=True):
    """Read-only boundary folder; absent own ancestry stays unknown.

    Callers must never use this profile for a mutation source or as evidence of
    a destination's placement. Complete immediate membership is still required
    for stored planning evidence. Live runtime parent reads validate membership
    separately through the transport's complete child listing.
    """
    if not isinstance(value, dict) or value.get("mime_type") != FOLDER:
        raise SafetyError("PARENT_CONTEXT_NOT_FOLDER")
    return _validated_snapshot(value, parent_context=True, require_subtree=require_subtree)


def _validated_snapshot(value, *, parent_context=False, require_subtree=True):
    """Validate known essentials; leave absent optional values absent, null as null."""
    if not isinstance(value, dict):
        raise SafetyError("INVALID_AGENT_METADATA")
    out = copy.deepcopy(value)
    if not _stable_id(out.get("id")) or not isinstance(out.get("name"), str) or not out["name"] or "\x00" in out["name"]:
        raise SafetyError("AGENT_EXACT_ID_AND_NAME_REQUIRED")
    if not isinstance(out.get("mime_type"), str) or not out["mime_type"]:
        raise SafetyError("AGENT_EXACT_MIME_REQUIRED")
    unknown_parents = (parent_context and out.get("parents") is None
                       and out.get("parents_complete") is False)
    if not unknown_parents:
        if out.get("parents_complete") is not True or not isinstance(out.get("parents"), list):
            raise SafetyError("AGENT_COMPLETE_PARENT_IDS_REQUIRED")
        _ids(out["parents"], "INVALID_PARENTS")
        if out["id"] in out["parents"]:
            raise SafetyError("SELF_PARENT")
        out["parents"] = sorted(out["parents"])
    if out.get("trashed") is True:
        raise SafetyError("TRASHED_SOURCE")
    if out.get("trashed") is not None and not isinstance(out["trashed"], bool):
        raise SafetyError("INVALID_TRASH_STATE")
    for field in ("permissions", "owners"):
        if out.get(field) is not None and not isinstance(out[field], list):
            raise SafetyError("INVALID_AGENT_" + field.upper())
    for field in ("capabilities", "access"):
        if out.get(field) is not None and not isinstance(out[field], dict):
            raise SafetyError("INVALID_AGENT_" + field.upper())
    if out["mime_type"] == FOLDER and require_subtree:
        subtree = out.get("subtree")
        if not isinstance(subtree, dict) or subtree.get("complete") is not True or not isinstance(subtree.get("members"), list):
            raise SafetyError("INCOMPLETE_DESCENDANTS")
        member_ids = []
        for member in subtree["members"]:
            if not isinstance(member, dict) or not _stable_id(member.get("id")):
                raise SafetyError("INVALID_DESCENDANT_ID")
            member_ids.append(member["id"])
        if len(member_ids) != len(set(member_ids)) or out["id"] in member_ids:
            raise SafetyError("INVALID_DESCENDANT_SCOPE")
    elif out["mime_type"] != FOLDER and out.get("subtree") is not None:
        subtree = out["subtree"]
        if not isinstance(subtree, dict) or subtree.get("members", []) != []:
            raise SafetyError("NONFOLDER_DESCENDANTS")
    unavailable = {field for field in _OPTIONAL if field not in out or out[field] is None}
    if not isinstance(out.get("access"), dict) or out["access"].get("complete") is not True:
        unavailable.add("effective_access")
    if unknown_parents:
        unavailable.add("parents")
    out["metadata_unavailable"] = sorted(unavailable)
    out["observation_profile"] = "READ_ONLY_PARENT_CONTEXT" if unknown_parents else PROFILE
    return out


def agent_semantic(value):
    """Compare observed safety data; availability reporting is informational.

    Known permission/capability/access fields remain in the comparison. Missing
    versus present values are not silently treated as equivalent or as no access.
    """
    return {key: val for key, val in agent_snapshot(value).items() if key not in _INFORMATIONAL}


def agent_postimage_matches(op, intended, observed):
    """Accept verified MOVE access readback without pretending it was predicted.

    Inheritance can change grants/capabilities when a parent changes. The exact
    observed values remain the inverse baseline; all other fields still match.
    """
    expected, actual = agent_semantic(intended), agent_semantic(observed)
    if op.action == "MOVE":
        for field in ("permissions", "access", "capabilities"):
            expected.pop(field, None)
            actual.pop(field, None)
    return expected == actual


def adjust_descendant_renames(state, dependency_events, journal):
    """Apply only verified, known child-title changes to a container preimage.

    This does not discard names from membership evidence or accept arbitrary
    subtree changes. A member without an embedded name stays without one.
    """
    result = copy.deepcopy(state)
    members = {member["id"]: member for member in _members(result)}
    changes = {}
    for event in sorted(dependency_events, key=lambda event: event["seq"]):
        payload = event.get("payload", {})
        if event.get("kind") not in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"} or payload.get("action") != "RENAME":
            continue
        member = members.get(payload.get("file_id"))
        if member is None or "name" not in member:
            continue
        if not payload.get("before_ref") or not payload.get("observed_ref"):
            raise SafetyError("DESCENDANT_RENAME_EVIDENCE_REQUIRED")
        before = journal.read_evidence(payload["before_ref"])
        observed = journal.read_evidence(payload["observed_ref"])
        source_id = payload["file_id"]
        if before.get("id") != source_id or observed.get("id") != source_id or not isinstance(before.get("name"), str) or not isinstance(observed.get("name"), str):
            raise SafetyError("DESCENDANT_RENAME_IDENTITY_MISMATCH")
        if before.get("mime_type") != observed.get("mime_type") or before.get("parents") != observed.get("parents"):
            raise SafetyError("DESCENDANT_RENAME_SCOPE_MISMATCH")
        chain = changes.setdefault(source_id, [before["name"]])
        if chain[-1] != before["name"]:
            raise SafetyError("DESCENDANT_RENAME_EVIDENCE_CHAIN_MISMATCH")
        chain.append(observed["name"])
    for source_id, chain in changes.items():
        member = members[source_id]
        if member["name"] not in chain:
            raise SafetyError("DESCENDANT_RENAME_BASELINE_MISMATCH")
        member["name"] = chain[-1]
    return result


def validate_agent_operation(op):
    if not _stable_id(op.file_id) or op.file_id == WITHDRAWN_ID or not isinstance(op.operation_id, str) or not op.operation_id:
        raise SafetyError("EXCLUDED_OR_INVALID_OPERATION_ID")
    before = agent_snapshot(op.before)
    if before["id"] != op.file_id:
        raise SafetyError("ID_MISMATCH")
    if op.action not in {"MOVE", "RENAME"}:
        raise SafetyError("UNSUPPORTED_ACTION")
    if op.direction not in {"FORWARD", "ROLLBACK"}:
        raise SafetyError("INVALID_DIRECTION")
    if (op.direction == "ROLLBACK") != bool(op.original_operation_id):
        raise SafetyError("ROLLBACK_LINK_REQUIRED")
    collisions = _ids(getattr(op, "allowed_collision_ids", ()), "INVALID_COLLISION_SCOPE")
    if op.file_id in collisions or WITHDRAWN_ID in collisions:
        raise SafetyError("INVALID_COLLISION_SCOPE")
    field = "parents" if op.action == "MOVE" else "name"
    if not isinstance(op.target, dict) or set(op.target) != {field}:
        raise SafetyError("FIELD_SCOPE")
    if field == "parents":
        if not isinstance(op.target[field], list):
            raise SafetyError("INVALID_PARENTS")
        targets = _ids(op.target[field], "INVALID_PARENTS", empty=False)
        if op.file_id in targets or WITHDRAWN_ID in targets:
            raise SafetyError("INVALID_DESTINATION")
    else:
        validate_rename_name(op, before)
    after = copy.deepcopy(before)
    after.update(copy.deepcopy(op.target))
    agent_snapshot(after)
    return before


def validate_agent_plan(plan):
    if not all(isinstance(value, str) and value for value in (plan.run_id, plan.batch_id, plan.proposal_revision)):
        raise SafetyError("MISSING_PLAN_IDENTITY")
    if plan.run_id != plan.batch_id:
        raise SafetyError("RUN_MUST_EQUAL_BATCH")
    excluded = _ids(plan.excluded_ids, "INVALID_EXCLUSION_IDS") | {WITHDRAWN_ID}
    ancestors, seen, scopes = {}, {}, {}
    for op in plan.ordered():
        before = validate_agent_operation(op)
        if len(op.depends_on) != len(set(op.depends_on)) or any(not isinstance(dep, str) or not dep for dep in op.depends_on):
            raise SafetyError("INVALID_DEPENDENCIES")
        members = {m["id"] for m in _members(before)}
        scopes[op.operation_id] = {op.file_id} | set(before["parents"]) | set(op.target.get("parents", [])) | members
        if scopes[op.operation_id] & excluded:
            raise SafetyError("EXCLUDED")
        if set(getattr(op, "allowed_collision_ids", ())) & excluded:
            raise SafetyError("EXCLUDED_COLLISION_SCOPE")
        closure = set(op.depends_on)
        for dep in op.depends_on:
            closure |= ancestors[dep]
        ancestors[op.operation_id] = closure
        if op.file_id in seen and seen[op.file_id] not in closure:
            raise SafetyError("UNORDERED_SAME_FILE")
        seen[op.file_id] = op.operation_id
    for i, left in enumerate(plan.operations):
        for right in plan.operations[i + 1:]:
            lm = {m["id"] for m in _members(left.before)}
            rm = {m["id"] for m in _members(right.before)}
            if left.file_id in rm or right.file_id in lm:
                if left.operation_id not in ancestors[right.operation_id] and right.operation_id not in ancestors[left.operation_id]:
                    raise SafetyError("UNORDERED_FOLDER_OVERLAP")
    return plan


_ACTION_STATES = {"PREPARED", "ATTEMPTED", "UNCERTAIN", "VERIFIED_APPLIED", "RECONCILED_AT_TARGET",
                  "FAILED_NOT_APPLIED", "BLOCKED_CONFLICT", "SKIPPED"}
_VERIFIED = {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"}


def _require_frozen_agent(journal, plan, records):
    rows = [r for r in records if r["kind"] == "DRAFTED" and r["payload"].get("run_id") == plan.run_id]
    if (not rows or rows[-1]["payload"].get("profile") != PROFILE
            or rows[-1]["payload"].get("proposal_hash") != plan.proposal_hash
            or digest(journal.read_evidence(rows[-1]["payload"]["plan_ref"])) != plan.proposal_hash):
        raise SafetyError("FROZEN_AGENT_PLAN_MISMATCH")


def _verified_images(journal, plan, op, event):
    payload = event["payload"]
    expected = {"run_id": plan.run_id, "proposal_hash": plan.proposal_hash, "file_id": op.file_id,
                "action": op.action, "direction": op.direction}
    if any(payload.get(key) != value for key, value in expected.items()):
        raise SafetyError("ROLLBACK_EVENT_BINDING_MISMATCH")
    if any(not payload.get(key) for key in ("before_ref", "intended_ref", "observed_ref")):
        raise SafetyError("ROLLBACK_EXACT_IMAGES_REQUIRED")
    before, intended, after = [agent_snapshot(journal.read_evidence(payload[key]))
                               for key in ("before_ref", "intended_ref", "observed_ref")]
    if agent_semantic(before) != agent_semantic(op.before):
        raise SafetyError("ROLLBACK_PREIMAGE_MISMATCH")
    if (any(intended[key] != op.intended(before)[key] for key in ("id", "name", "mime_type", "parents"))
            or not agent_postimage_matches(op, intended, after)):
        raise SafetyError("ROLLBACK_EXACT_POSTIMAGE_REQUIRED")
    return before, after


def rollback_agent_plan(journal, forward_plan, selected_ids, batch_id):
    """Build a local compensation candidate, without approval or remote actions.

    The handoff still has to read live state and match each frozen postimage.
    Unknown requests anywhere in this store must be reconciled first. A prior
    successful compensation is never automatically redone.
    """
    with journal.locked():
        validate_agent_plan(forward_plan)
        if any(op.direction != "FORWARD" for op in forward_plan.operations):
            raise SafetyError("ROLLBACK_REQUIRES_FORWARD_PLAN")
        records = journal.records()
        _require_frozen_agent(journal, forward_plan, records)
        selected = set(selected_ids)
        lookup = {op.operation_id: op for op in forward_plan.operations}
        if not selected <= lookup.keys():
            raise SafetyError("UNKNOWN_ROLLBACK_SELECTION")
        requests, states = {}, {}
        for event in records:
            if event["kind"] in _ACTION_STATES and event["operation_id"]:
                key = event["payload"].get("envelope_hash") or event["payload"].get("attempt_key") or (event["operation_id"], event["attempt"])
                requests[key] = event
                states[event["operation_id"]] = event
        if any(event["kind"] in {"ATTEMPTED", "UNCERTAIN"}
               or (event["kind"] == "PREPARED" and event["payload"].get("bridge")) for event in requests.values()):
            raise SafetyError("RECONCILE_BEFORE_ROLLBACK")
        reverted = {event["payload"].get("original_operation_id") for event in records
                    if event["kind"] in _VERIFIED and event["payload"].get("direction") == "ROLLBACK"}
        verified = {event["operation_id"]: event for event in records
                    if event["kind"] in _VERIFIED and event["operation_id"] in lookup
                    and event["payload"].get("direction") == "FORWARD"}
        for op_id in verified.keys() - reverted:
            if states[op_id]["kind"] not in _VERIFIED:
                raise SafetyError("ROLLBACK_STATE_REQUIRES_RECONCILIATION")
        active = verified.keys() - reverted
        selected &= active
        closure = {}
        for op in forward_plan.ordered():
            closure[op.operation_id] = set(op.depends_on)
            for dep in op.depends_on:
                closure[op.operation_id] |= closure[dep]
            if op.operation_id in active - selected and closure[op.operation_id] & selected:
                raise SafetyError("ROLLBACK_HAS_ACTIVE_DEPENDENTS")
        images = {op_id: _verified_images(journal, forward_plan, lookup[op_id], verified[op_id]) for op_id in selected}
        # Among reverse-topological candidates, preserve reverse actual event order.
        remaining, reverse_order = set(selected), []
        while remaining:
            ready = [op_id for op_id in remaining if not any(op_id in closure[other] for other in remaining)]
            next_id = max(ready, key=lambda op_id: verified[op_id]["seq"])
            reverse_order.append(next_id)
            remaining.remove(next_id)
        operations, simulated, reversed_renames = [], {}, []
        for op_id in reverse_order:
            op = lookup[op_id]
            before, after = images[op_id]
            baseline = copy.deepcopy(simulated.get(op.file_id, after))
            for child_id, old_name, new_name in reversed_renames:
                for member in _members(baseline):
                    if member["id"] == child_id and "name" in member:
                        if member["name"] == old_name:
                            member["name"] = new_name
                        elif member["name"] != new_name:
                            raise SafetyError("ROLLBACK_DESCENDANT_CHAIN_DRIFT")
            if agent_semantic(baseline) != agent_semantic(after):
                raise SafetyError("ROLLBACK_CHAIN_DRIFT")
            target = {"name": before["name"]} if op.action == "RENAME" else {"parents": before["parents"]}
            deps = (operations[-1].operation_id,) if operations else ()
            operations.append(Operation(f"{batch_id}:undo:{op_id}", op.file_id, op.action,
                                        copy.deepcopy(baseline), copy.deepcopy(target), deps,
                                        op.proposal_refs, "ROLLBACK", op_id))
            simulated[op.file_id] = copy.deepcopy(before)
            for key in ("version", "modified_time"):
                if key in baseline:
                    simulated[op.file_id][key] = baseline[key]
            if op.action == "RENAME":
                reversed_renames.append((op.file_id, after["name"], before["name"]))
        result = Plan(batch_id, batch_id, "rollback:" + forward_plan.proposal_revision,
                      tuple(operations), forward_plan.excluded_ids)
        validate_agent_plan(result)
        validate_rollback_links(journal, result)
        return result


# Kept as a descriptive alias for orchestration callers.
agent_rollback_plan = rollback_agent_plan


def register_agent_plan(journal, plan):
    """Seal candidate evidence only. Registration grants no execution authority."""
    with journal.locked():
        validate_agent_plan(plan)
        for op in plan.operations:
            validate_extension_evidence(journal, op)
        validate_rollback_links(journal, plan)
        records = journal.records()
        from .shortcut_policy import excluded_shortcut_ids
        policy_excluded = excluded_shortcut_ids(records)
        excluded = {WITHDRAWN_ID} | set(plan.excluded_ids) | policy_excluded | {r["payload"]["file_id"] for r in records if r["kind"] == "EXCLUDED"}
        excluded_refs = {r["payload"]["proposal_ref"] for r in records if r["kind"] == "PROPOSAL_EXCLUDED"}
        for item in plan.excluded_ids:
            # Existing typed policy is already durable execution exclusion. Do
            # not turn it into a generic privacy/withdrawal exclusion.
            if item in policy_excluded:
                continue
            if not any(r["kind"] == "EXCLUDED" and r["payload"]["file_id"] == item for r in records):
                journal.append("EXCLUDED", payload={"file_id": item, "reason_code": "FROZEN_PLAN_EXCLUSION"})
        scope = set()
        for op in plan.operations:
            if set(op.proposal_refs) & excluded_refs:
                raise SafetyError("PROPOSAL_EXCLUDED")
            op_scope = {op.file_id} | set(op.before["parents"]) | set(op.target.get("parents", []))
            op_scope |= {m["id"] for m in _members(op.before)}
            if op_scope & excluded or set(getattr(op, "allowed_collision_ids", ())) & excluded:
                raise SafetyError("EXCLUDED")
            scope |= op_scope
        existing = [r for r in records if r["kind"] == "DRAFTED" and r["payload"]["run_id"] == plan.run_id]
        if existing:
            prior = existing[-1]
            if prior["payload"]["proposal_hash"] != plan.proposal_hash or prior["payload"].get("profile") != PROFILE:
                raise SafetyError("BATCH_ALREADY_FROZEN")
            if journal.read_evidence(prior["payload"]["plan_ref"]) != plan.body():
                # JSON round-trips tuples as lists; canonical hash is the binding.
                from .model import digest
                if digest(journal.read_evidence(prior["payload"]["plan_ref"])) != plan.proposal_hash:
                    raise SafetyError("FROZEN_PLAN_EVIDENCE_MISMATCH")
            return prior
        taken = {r["operation_id"] for r in records if r["operation_id"]}
        latest = {}
        for record in records:
            if record["kind"] == "DRAFTED":
                taken |= {op["operation_id"] for op in journal.read_evidence(record["payload"]["plan_ref"])["operations"]}
            if record["operation_id"] and record["kind"] in {"PREPARED", "ATTEMPTED", "UNCERTAIN", "VERIFIED_APPLIED", "RECONCILED_AT_TARGET", "FAILED_NOT_APPLIED", "BLOCKED_CONFLICT", "SKIPPED"}:
                latest[record["operation_id"]] = record
        if taken & {op.operation_id for op in plan.operations}:
            raise SafetyError("OPERATION_ID_REUSED")
        reconciled_history = None
        for record in latest.values():
            if record["kind"] not in {"ATTEMPTED", "UNCERTAIN"} and not (record["kind"] == "PREPARED" and record["payload"].get("bridge")):
                continue
            previous_scope = {record["payload"]["file_id"]} | set(record["payload"].get("scope_ids", []))
            if record["payload"].get("before_ref"):
                old = journal.read_evidence(record["payload"]["before_ref"])
                previous_scope |= set(old.get("parents", [])) | {m["id"] for m in _members(old)}
            if previous_scope & scope:
                source_id = record["payload"]["file_id"]
                if record["kind"] in {"ATTEMPTED", "UNCERTAIN"} and any(
                        later["seq"] > record["seq"] and later["kind"] == "DESIRED_APPLIED"
                        and later["payload"].get("source_id") == source_id for later in records):
                    # Desired-state recovery may have verified an old adapter's
                    # effect without rewriting its historical uncertain event.
                    # Reuse the evidence-validated cross-adapter history; a bare
                    # receipt, unrelated success or conflicting target cannot
                    # release this fence. PREPARED handoffs remain fenced.
                    if reconciled_history is None:
                        from .destination_history import build_history_index
                        reconciled_history = build_history_index(journal)
                    history = reconciled_history.get(source_id, {})
                    if (record["event_id"] in history.get("event_ids", [])
                            and record["operation_id"] in history.get("historical_uncertain_operation_ids", [])
                            and not history.get("pending_requests", [None])):
                        continue
                raise SafetyError("UNRESOLVED_PRIOR_REQUEST")
        ref = journal.evidence(plan.body())
        return journal.append("DRAFTED", payload={"run_id": plan.run_id, "batch_id": plan.batch_id,
            "proposal_revision": plan.proposal_revision, "proposal_hash": plan.proposal_hash,
            "plan_ref": ref, "profile": PROFILE, "execution_authorized": False})
