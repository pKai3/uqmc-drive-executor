"""Local, one-way projections. No connector, server, or credentials are used here.

The journal is authoritative. Exporting, delivering, or retrying a projection can
never call a Drive operation. The public payload is built from an allowlist and
does not include private evidence or source IDs.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone


HEADERS = [
    "Event ID", "Event UTC", "Run ID", "Operation ID", "Attempt", "Operation order",
    "Depends on operation IDs", "Direction", "Action", "Proposal references", "Item ID",
    "MIME type", "Before exact name", "Before parent IDs", "Intended exact name",
    "Intended parent IDs", "State", "Observed exact name", "Observed parent IDs",
    "Prewrite evidence ref", "Verification evidence ref", "Permission snapshot ref",
    "Related forward operation ID", "Actor", "Result / conflict / error",
    "Record hash / sealed event ref",
]
PUBLICATION_KINDS = frozenset({
    "PUBLICATION_QUEUED", "PUBLICATION_SUCCEEDED", "PUBLICATION_FAILED",
})
WORKBOOK_STATES = frozenset({"PREPARED", "ATTEMPTED", "VERIFIED_APPLIED",
                             "FAILED_NOT_APPLIED", "UNCERTAIN", "BLOCKED_CONFLICT", "SKIPPED"})
ACTION_STATES = frozenset({
    "PREPARED", "ATTEMPTED", "VERIFIED_APPLIED", "RECONCILED_AT_TARGET",
    "FAILED_NOT_APPLIED", "UNCERTAIN", "BLOCKED_CONFLICT", "SKIPPED", "EXCLUDED",
})
REASON_CODES = frozenset({
    "PRECONDITION_CHANGED", "PERMISSION_CHANGED", "ACCESS_UNKNOWN", "ACCESS_CHANGED",
    "DEPENDENCY_BLOCKED", "DEPENDENCY_UNSATISFIED", "UNKNOWN_OUTCOME", "MANUAL_DRIFT",
    "APPROVAL_REQUIRED", "APPROVAL_REVOKED", "APPROVAL_MISMATCH", "APPROVAL_EXPIRED",
    "EXCLUDED", "WITHDRAWN", "COLLISION", "METADATA_INCOMPLETE", "SOURCE_MISSING",
    "DESTINATION_MISSING", "PARENT_MISSING", "ALREADY_AT_BASELINE", "ALREADY_AT_TARGET",
    "RECONCILED_TARGET", "OBSERVED_TARGET", "RETRY_FENCE", "LIVE_DISABLED",
    "READBACK_FAILED", "READBACK_MISMATCH", "NOT_APPLIED", "NO_CHANGE",
    "INVALID_PLAN", "JOURNAL_FAILURE", "DELIVERY_FAILED", "NONE",
    "OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN", "NO_SYSTEM_RECORD",
    "EXACT_POSTIMAGE_VERIFIED", "TERMINAL_AND_PREIMAGE_VERIFIED", "RECONCILIATION_DRIFT",
    "REQUEST_NOT_PROVEN_TERMINAL", "RECONCILIATION_METADATA_BLOCKED", "READ_UNAVAILABLE",
    "ALREADY_AT_APPROVED_STATE", "BASELINE_DRIFT", "PRE_DISPATCH_DRIFT", "POST_ACTION_DRIFT",
    "DISPATCH_OR_READBACK_FAILED", "ACCESS_CHANGE_REQUIRES_EXPLICIT_APPROVAL",
    "INCOMPLETE_METADATA", "INCOMPLETE_EFFECTIVE_ACCESS", "INCOMPLETE_DESCENDANTS",
    "UNRESOLVED_PRIOR_REQUEST", "LIVE_EXECUTION_DISABLED", "STALE_APPROVAL",
})
_HASH = re.compile(r"^[a-f0-9]{64}$")
_REF = re.compile(r"^sha256:[a-f0-9]{64}$")


def _canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(data):
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def _opaque(kind, value):
    return kind + ":" + _hash([kind, value])


def _safe_hash(value):
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def _safe_ref(value):
    return value if isinstance(value, str) and _REF.fullmatch(value) else ""


def _reason(value):
    return value if isinstance(value, str) and value in REASON_CODES else "REVIEW_REQUIRED"


def source_head(records):
    """Publication bookkeeping is not an action-state change."""
    for event in reversed(records):
        if event["kind"] not in PUBLICATION_KINDS:
            return {"seq": event["seq"], "hash": event["hash"]}
    return {"seq": 0, "hash": None}


def _snapshot(journal, ref):
    if not ref:
        return {}
    if not _safe_ref(ref):
        raise ValueError("Invalid private evidence reference")
    value = journal.read_evidence(ref)
    if not isinstance(value, dict):
        raise ValueError("Snapshot evidence must be an object")
    return value


def _json_cell(value):
    # Missing is deliberately blank, not a fabricated empty parent set.
    return "" if value is None else _canonical(value)


def build_projections(journal, *, allowed_source_ids=(), current_source_head=None):
    """Return separate private workbook and redacted HTML JSON payloads.

    No file or remote resource is written. A caller may subsequently persist each
    payload in its appropriate location. `current_source_head` is an optional
    independently observed journal watermark used to report known staleness.
    Neither projection establishes current live Drive state.
    """
    with journal.locked():
        records = journal.records()
        head = source_head(records)
        excluded_ids = {e.get("payload", {}).get("file_id") for e in records if e["kind"] == "EXCLUDED"}
        rows, operations, item_events, run_approvals = [], {}, {}, {}
        ancestor_effects, ancestor_unknown = {}, {}
        approval_runs, approval_hashes = {}, {}
        for event in records:
            kind = event["kind"]
            if kind in PUBLICATION_KINDS:
                continue
            p = event.get("payload", {})
            run_id = p.get("batch_id", p.get("run_id", ""))
            if kind == "APPROVED":
                approval_runs[p.get("approval_id")] = run_id
                run_approvals[run_id] = "APPROVED"
                approval_hashes[run_id] = _safe_hash(p.get("proposal_hash"))
                if p.get("approval_ref"):
                    approval = _snapshot(journal, p["approval_ref"])
                    try:
                        expires = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
                        if expires <= datetime.now(timezone.utc):
                            run_approvals[run_id] = "STALE"
                    except (KeyError, ValueError, TypeError):
                        run_approvals[run_id] = "STALE"
            elif kind == "REVOKED":
                run_approvals[approval_runs.get(p.get("approval_id"), run_id)] = "WITHDRAWN"
            elif kind == "DRAFTED" and p.get("plan_ref"):
                plan = _snapshot(journal, p["plan_ref"])
                for planned in plan.get("operations", []):
                    planned_id = planned["operation_id"]
                    seed = {**p, **{k: v for k, v in planned.items() if k not in {"before", "target"}}}
                    seed["run_id"] = run_id
                    seed_event = {**event, "operation_id": planned_id, "payload": seed}
                    operations.setdefault((run_id, planned_id), {
                        "payload": seed, "state": "NOT_ATTEMPTED", "approval_state": "NOT_APPROVED",
                        "events": [], "last_event": seed_event})
                    item_events.setdefault(planned["file_id"], []).append((seed_event, dict(seed)))
            op_id = event.get("operation_id")
            if not op_id or p.get("action") not in {"MOVE", "RENAME"}:
                continue
            key = (run_id, op_id)
            op = operations.setdefault(key, {"payload": {}, "state": "NOT_ATTEMPTED",
                                             "approval_state": "NOT_APPROVED", "events": []})
            op["payload"].update(p)
            state = op["payload"]
            state["run_id"] = run_id
            if kind in ACTION_STATES:
                # Exclusions are permanent in presentation as well as execution.
                if op["state"] != "EXCLUDED":
                    op["state"] = kind
            if kind in {"APPROVED", "REVOKED"}:
                op["approval_state"] = "APPROVED" if kind == "APPROVED" else "WITHDRAWN"
            if kind == "ATTEMPTED":
                op["issue_order"] = event["seq"]
            op["last_event"] = event
            op["events"].append(event)
            item_events.setdefault(state.get("file_id", ""), []).append((event, dict(state)))
            if kind not in WORKBOOK_STATES and kind != "RECONCILED_AT_TARGET":
                continue
            before = _snapshot(journal, state.get("before_ref"))
            intended = _snapshot(journal, state.get("intended_ref"))
            observed = _snapshot(journal, state.get("observed_ref"))
            if state["action"] == "MOVE" and before.get("mime_type") == "application/vnd.google-apps.folder":
                subtree = before.get("subtree", {})
                members = {m["id"] for m in subtree.get("members", []) if "id" in m}
                observed_subtree = observed.get("subtree", {})
                observed_members = {m["id"] for m in observed_subtree.get("members", []) if "id" in m}
                for member in members:
                    ancestor_effects.setdefault(member, set())
                    ancestor_unknown.setdefault(member, set())
                if kind in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"}:
                    complete = (subtree.get("complete") is True and
                                observed_subtree.get("complete") is True and members == observed_members)
                    for member in members:
                        if not complete:
                            ancestor_unknown[member].add(op_id)
                        elif state.get("direction", "FORWARD") == "ROLLBACK":
                            ancestor_effects[member].discard(state.get("original_operation_id"))
                        else:
                            ancestor_effects[member].add(op_id)
                    if complete:
                        for member in members:
                            ancestor_unknown[member].discard(op_id)
                            if state.get("direction") == "ROLLBACK":
                                ancestor_unknown[member].discard(state.get("original_operation_id"))
                elif kind in {"ATTEMPTED", "UNCERTAIN", "BLOCKED_CONFLICT"}:
                    for member in members:
                        ancestor_unknown[member].add(op_id)
                elif kind == "FAILED_NOT_APPLIED":
                    for member in members:
                        ancestor_unknown[member].discard(op_id)
            rows.append([
                event["event_id"], event["at"], state.get("run_id", ""), op_id,
                event.get("attempt", 0), state.get("operation_order", op.get("issue_order", "")),
                _json_cell(state.get("depends_on", [])),
                state.get("direction", "FORWARD"), state["action"],
                _json_cell(state.get("proposal_refs", [])), state.get("file_id", ""),
                before.get("mime_type", observed.get("mime_type", "")),
                before.get("name", ""), _json_cell(before.get("parents")),
                intended.get("name", ""), _json_cell(intended.get("parents")),
                "VERIFIED_APPLIED" if kind == "RECONCILED_AT_TARGET" else kind,
                observed.get("name", ""), _json_cell(observed.get("parents")),
                _safe_ref(state.get("before_ref")), _safe_ref(state.get("observed_ref")),
                _safe_ref(state.get("permission_ref") or state.get("before_ref")),
                state.get("original_operation_id") or "", state.get("actor", ""),
                ("OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN" if kind == "RECONCILED_AT_TARGET"
                 else _reason(p.get("reason_code", "NONE"))), event["hash"],
            ])

    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    stale = None if current_source_head is None else current_source_head != head
    freshness = "SNAPSHOT_ONLY" if stale is None else ("STALE" if stale else "JOURNAL_CURRENT")
    common = {"schema_version": "uqmc.execution.snapshot.v1", "snapshot_id": None,
              "journal_sequence": head["seq"], "journal_tip_sha256": head["hash"],
              "generated_at_utc": generated, "stale": stale, "freshness": freshness,
              "coverage": {"basis": "tracked_operations_only", "last_reconciled_at_utc": None,
                           "complete": False}}
    projected_ops = []
    for (run_id, op_id), op in operations.items():
        p = op["payload"]
        latest = op["last_event"]
        verified_events = [e for e in op["events"]
                           if e["kind"] in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"}]
        direction = p.get("direction", "FORWARD")
        if direction not in {"FORWARD", "ROLLBACK"}:
            raise ValueError("Unsupported operation direction")
        state = op["state"]
        projected_ops.append({
            "operation_id": op_id, "run_id": run_id, "source_id": p.get("file_id"),
            "action": p["action"], "direction": direction,
            "state": "VERIFIED_APPLIED" if state == "RECONCILED_AT_TARGET" else state,
            "operation_order": p.get("operation_order", op.get("issue_order")),
            "depends_on_operation_ids": p.get("depends_on", []),
            "approval_state": ("WITHDRAWN" if state == "EXCLUDED" or p.get("file_id") in excluded_ids else
                               run_approvals.get(run_id, op["approval_state"])),
            "approved_plan_sha256": approval_hashes.get(run_id),
            "related_forward_operation_id": p.get("original_operation_id"),
            "verified_at_utc": verified_events[-1]["at"] if verified_events else None,
            "reason_code": ("OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN" if state == "RECONCILED_AT_TARGET"
                            else _reason(p.get("reason_code", "NONE"))),
        })

    items = []
    # The explicit allowlist can include untracked source IDs without pretending
    # that a missing system record proves their real location.
    for file_id in dict.fromkeys([*item_events, *ancestor_effects, *allowed_source_ids]):
        history = item_events.get(file_id, [])
        item = {"source_id": file_id, "reason_codes": [],
                "location": {"effect": "UNKNOWN", "ancestor_move_operation_ids": [],
                             "verified_path": None, "path_verified_at_utc": None},
                "observed": {"exact_name": None, "parent_ids": None, "mime_type": None,
                             "metadata_verified_at_utc": None, "read_state": "NEVER_VERIFIED"}}
        for action in ("MOVE", "RENAME"):
            action_history = [(e, p) for e, p in history if p["action"] == action]
            applied, last_effect, last_state, verified_at = set(), "NOT_TRACKED", None, None
            op_ids = []
            for event, p in action_history:
                op_id = event["operation_id"]
                if op_id not in op_ids:
                    op_ids.append(op_id)
                kind = event["kind"]
                if kind in ACTION_STATES:
                    last_state = "VERIFIED_APPLIED" if kind == "RECONCILED_AT_TARGET" else kind
                if last_effect == "NOT_TRACKED":
                    last_effect = "UNCHANGED"
                if kind in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"}:
                    if p.get("direction", "FORWARD") == "ROLLBACK":
                        applied.discard(p.get("original_operation_id"))
                        last_effect = "APPLIED" if applied else "REVERTED"
                    else:
                        applied.add(op_id)
                        last_effect = "APPLIED"
                    verified_at = event["at"]
                elif kind in {"UNCERTAIN", "ATTEMPTED"}:
                    last_effect = "UNKNOWN"
                elif kind == "BLOCKED_CONFLICT":
                    last_effect = "CONFLICT"
                elif kind == "FAILED_NOT_APPLIED":
                    # Preserve established earlier success when the later
                    # attempt is verified to have made no change.
                    last_effect = "APPLIED" if applied else ("REVERTED" if verified_at else "UNCHANGED")
            item[action.lower()] = {"effect": last_effect, "last_event_state": last_state,
                                    "operation_ids": op_ids, "verified_at_utc": verified_at}
        for event, p in history:
            reason = _reason(p.get("reason_code", "NONE"))
            if reason != "NONE" and reason not in item["reason_codes"]:
                item["reason_codes"].append(reason)
            if event["kind"] in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET", "FAILED_NOT_APPLIED"}:
                observed = _snapshot(journal, p.get("observed_ref"))
                if observed:
                    item["observed"] = {"exact_name": observed.get("name"),
                                        "parent_ids": observed.get("parents"),
                                        "mime_type": observed.get("mime_type"),
                                        "metadata_verified_at_utc": event["at"],
                                        "read_state": "VERIFIED"}
            elif event["kind"] == "BLOCKED_CONFLICT":
                item["observed"]["read_state"] = "CONFLICT"
            elif event["kind"] in {"UNCERTAIN", "ATTEMPTED"}:
                item["observed"]["read_state"] = "UNAVAILABLE"
        if not history:
            item["reason_codes"].append("NO_SYSTEM_RECORD")
        if file_id in excluded_ids:
            item["reason_codes"].append("WITHDRAWN")
        ancestors = sorted(ancestor_effects.get(file_id, set()))
        item["location"]["ancestor_move_operation_ids"] = ancestors
        if ancestor_unknown.get(file_id) or item["move"]["effect"] in {"UNKNOWN", "CONFLICT"}:
            item["location"]["effect"] = "UNKNOWN"
        elif item["move"]["effect"] == "APPLIED":
            item["location"]["effect"] = "DIRECT_AND_INHERITED" if ancestors else "DIRECT"
        elif ancestors:
            item["location"]["effect"] = "INHERITED"
        elif file_id in ancestor_effects or item["move"]["effect"] == "REVERTED":
            item["location"]["effect"] = "NONE"
        items.append(item)

    workbook = {
        **common, "kind": "private_workbook_events", "classification": "PRIVATE",
        "sheet_name": "Operation Log", "valueInputOption": "RAW",
        "idempotency_column": "Event ID", "headers": list(HEADERS), "rows": rows,
        "operations": projected_ops, "items": items,
    }
    allowed = set(allowed_source_ids) - excluded_ids
    public_ops = [dict(op) for op in projected_ops if op["source_id"] in allowed]
    allowed_op_ids = {op["operation_id"] for op in public_ops}
    for op in public_ops:
        op["depends_on_operation_ids"] = [v for v in op["depends_on_operation_ids"] if v in allowed_op_ids]
        if op["related_forward_operation_id"] not in allowed_op_ids:
            op["related_forward_operation_id"] = None
    public_items = json.loads(json.dumps([i for i in items if i["source_id"] in allowed]))
    for item in public_items:
        item["observed"]["exact_name"] = None
        item["observed"]["parent_ids"] = None
        item["observed"]["mime_type"] = None
        item["location"]["ancestor_move_operation_ids"] = [
            v for v in item["location"]["ancestor_move_operation_ids"] if v in allowed_op_ids]
    html = {**common, "kind": "public_status_projection", "classification": "REDACTED",
            "operations": public_ops, "items": public_items, "contains_private_evidence": False,
            "execution_authority": False}
    # A retry after only one surface delivered must not invent a new pair ID.
    # Approval expiry or a changed allowlist changes the projection content and
    # therefore creates a new snapshot even at the same journal watermark.
    nonvolatile = lambda value: {k: v for k, v in value.items() if k not in {
        "snapshot_id", "generated_at_utc", "stale", "freshness"}}
    pair_hash = _hash({"workbook": nonvolatile(workbook), "html": nonvolatile(html)})
    pair_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "uqmc.execution.snapshot.v1:" + pair_hash))
    workbook["snapshot_id"] = html["snapshot_id"] = pair_id
    return {"workbook": workbook, "html": html}


def delivery_key(destination, payload):
    """Stable across regenerated timestamps and repeated delivery attempts."""
    if destination not in {"workbook", "html"}:
        raise ValueError("Unknown projection destination")
    stable = {k: v for k, v in payload.items() if k not in {
        "generated_at_utc", "stale", "freshness"}}
    return "delivery:" + _hash([destination, stable])


def deliver_projection(journal, destination, payload, sink):
    """Call an explicitly supplied projection sink, never a Drive executor.

    This generic helper has no configured live sink. A sink must deduplicate by
    delivery key, and workbook sinks must additionally deduplicate each Event ID.
    After uncertain sink completion the same key is retried; sink readback is
    required before acknowledging success. A crash may therefore cause repeated
    delivery attempts, never exactly-once claims.
    """
    key = delivery_key(destination, payload)
    binding = {"delivery_key": key, "destination": destination,
               "as_of": {"seq": payload["journal_sequence"], "hash": payload["journal_tip_sha256"]}}
    with journal.locked():
        if any(e["kind"] == "PUBLICATION_SUCCEEDED" and
               e.get("payload", {}).get("delivery_key") == key for e in journal.records()):
            return {**binding, "state": "ALREADY_DELIVERED"}
        binding["projection_ref"] = journal.evidence(payload)
        journal.append("PUBLICATION_QUEUED", payload=binding)
        try:
            receipt = sink(key, payload)
            if not isinstance(receipt, dict) or receipt.get("verified") is not True:
                raise ValueError("Sink has not confirmed delivery by readback")
        except Exception:
            # Raw exception text may contain secrets, names, or private URLs.
            journal.append("PUBLICATION_FAILED", payload={**binding, "reason_code": "DELIVERY_FAILED"})
            return {**binding, "state": "PUBLICATION_FAILED"}
        journal.append("PUBLICATION_SUCCEEDED", payload=binding)
        return {**binding, "state": "PUBLICATION_SUCCEEDED"}
