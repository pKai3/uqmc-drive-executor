"""Local, one-way projections. No connector, server, or credentials are used here.

The journal is authoritative. Exporting, delivering, or retrying a projection can
never call a Drive operation. The public payload uses an explicit source-ID
allowlist and does not include private evidence.
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


def _identity_coverage(basis, allowed_source_ids):
    """Bind a declared identity basis to its actual IDs, never just its count."""
    selected = list(allowed_source_ids)
    if basis is None:
        # Local API/fixture default describes only this explicit disclosure set.
        # The real CLI requires an externally supplied, verified basis file.
        basis = {"identity_basis_version": 1, "source_ids": selected}
    ids = basis.get("source_ids")
    version = basis.get("identity_basis_version")
    if (not isinstance(ids, list) or any(not isinstance(i, str) or not i or "\n" in i or "\r" in i for i in ids)
            or len(ids) != len(set(ids)) or type(version) is not int or version < 1):
        raise ValueError("Invalid identity basis")
    if not set(selected) <= set(ids):
        raise ValueError("Disclosure selection is outside identity basis")
    fingerprint = hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()
    if basis.get("source_identity_sha256", fingerprint) != fingerprint:
        raise ValueError("Identity basis fingerprint mismatch")
    if (type(basis.get("identity_count", len(ids))) is not int
            or basis.get("identity_count", len(ids)) != len(ids)):
        raise ValueError("Identity basis count mismatch")
    return {"identity_basis_version": version, "identity_count": len(ids),
            "source_identity_sha256": fingerprint}


def validate_snapshot(value):
    """Strict, dependency-free validation of the shared v1 export contract."""
    def obj(item, keys):
        if not isinstance(item, dict) or set(item) != set(keys.split()):
            raise ValueError("Snapshot object has missing or additional fields")

    def string(item, nullable=False, minimum=0):
        if item is None and nullable:
            return
        if not isinstance(item, str) or len(item) < minimum:
            raise ValueError("Snapshot string required")

    def strings(items):
        if not isinstance(items, list):
            raise ValueError("Snapshot list required")
        for item in items:
            string(item)
        if len(items) != len(set(items)):
            raise ValueError("Snapshot array items must be unique")

    def objects(items):
        if not isinstance(items, list):
            raise ValueError("Snapshot list required")
        fingerprints = [_canonical(item) for item in items]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("Snapshot array items must be unique")

    def integer(item, minimum=0):
        if type(item) is not int or item < minimum:
            raise ValueError("Snapshot integer out of bounds")

    def timestamp(item, nullable=True):
        if item is None and nullable:
            return
        string(item)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", item):
            raise ValueError("UTC timestamp required")
        datetime.fromisoformat(item[:-1] + "+00:00")

    obj(value, "schema_version snapshot_id generated_at_utc journal_sequence journal_tip_sha256 coverage operations items")
    if value["schema_version"] != "uqmc.execution.snapshot.v1":
        raise ValueError("Unsupported snapshot schema")
    if not isinstance(value["snapshot_id"], str):
        raise ValueError("Snapshot UUID required")
    uuid.UUID(value["snapshot_id"])
    timestamp(value["generated_at_utc"], False)
    integer(value["journal_sequence"])
    string(value["journal_tip_sha256"], True)
    coverage = value["coverage"]
    obj(coverage, "basis last_reconciled_at_utc complete identity_basis_version identity_count source_identity_sha256")
    if coverage["basis"] != "tracked_operations_only" or type(coverage["complete"]) is not bool:
        raise ValueError("Invalid snapshot coverage")
    timestamp(coverage["last_reconciled_at_utc"])
    integer(coverage["identity_basis_version"], 1)
    integer(coverage["identity_count"])
    string(coverage["source_identity_sha256"])
    objects(value["operations"])
    objects(value["items"])
    for operation in value["operations"]:
        obj(operation, "operation_id run_id source_id action direction state operation_order depends_on_operation_ids approval_state approved_plan_sha256 related_forward_operation_id verified_at_utc reason_code")
        for key in ("operation_id", "run_id"):
            string(operation[key], minimum=1)
        for key in ("source_id", "reason_code"):
            string(operation[key], nullable=True)
        if (operation["action"] not in {"MOVE", "RENAME", "MOVE_AND_RENAME", "CREATE_FOLDER"}
                or operation["direction"] not in {"FORWARD", "ROLLBACK"}
                or operation["state"] not in WORKBOOK_STATES
                or operation["approval_state"] not in {"NOT_APPROVED", "APPROVED", "STALE", "WITHDRAWN"}):
            raise ValueError("Invalid operation enum")
        integer(operation["operation_order"], 1)
        strings(operation["depends_on_operation_ids"])
        string(operation["approved_plan_sha256"], True)
        string(operation["related_forward_operation_id"], True)
        timestamp(operation["verified_at_utc"])
    for item in value["items"]:
        obj(item, "source_id source_link move rename location observed reason_codes")
        string(item["source_id"], minimum=1)
        string(item["source_link"], True)
        strings(item["reason_codes"])
        for field in ("move", "rename"):
            effect = item[field]
            obj(effect, "effect last_event_state operation_ids verified_at_utc")
            if effect["effect"] not in {"NOT_TRACKED", "UNCHANGED", "APPLIED", "REVERTED", "UNKNOWN", "CONFLICT"}:
                raise ValueError("Invalid effect")
            if effect["last_event_state"] is not None and effect["last_event_state"] not in WORKBOOK_STATES:
                raise ValueError("Invalid last event state")
            strings(effect["operation_ids"])
            timestamp(effect["verified_at_utc"])
        location = item["location"]
        obj(location, "effect ancestor_move_operation_ids verified_path path_verified_at_utc")
        if location["effect"] not in {"NONE", "DIRECT", "INHERITED", "DIRECT_AND_INHERITED", "UNKNOWN"}:
            raise ValueError("Invalid location effect")
        strings(location["ancestor_move_operation_ids"])
        string(location["verified_path"], True)
        timestamp(location["path_verified_at_utc"])
        observed = item["observed"]
        obj(observed, "exact_name parent_ids mime_type metadata_verified_at_utc read_state")
        string(observed["exact_name"], True)
        string(observed["mime_type"], True)
        if observed["parent_ids"] is not None:
            strings(observed["parent_ids"])
        timestamp(observed["metadata_verified_at_utc"])
        if observed["read_state"] not in {"NEVER_VERIFIED", "VERIFIED", "UNAVAILABLE", "CONFLICT"}:
            raise ValueError("Invalid observation state")
    return value


def _ordered_plan_operations(plan):
    """The stable topological order used by Plan.ordered(), from sealed JSON."""
    pending = list(plan.get("operations", []))
    ids = [op["operation_id"] for op in pending]
    if any(not isinstance(op_id, str) or not op_id for op_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Invalid sealed operation identities")
    result, completed = [], set()
    while pending:
        ready = [op for op in pending if set(op.get("depends_on", [])) <= completed]
        if not ready:
            raise ValueError("Sealed plan dependency cycle or missing operation")
        for op in ready:
            result.append(op)
            completed.add(op["operation_id"])
            pending.remove(op)
    return result


def build_projections(journal, *, allowed_source_ids=(), current_source_head=None,
                      identity_basis=None, verified_source_links=None):
    """Return separate private workbook and redacted HTML JSON payloads.

    No file or remote resource is written. A caller may subsequently persist each
    payload in its appropriate location. `current_source_head` is an optional
    independently observed journal watermark used to report known staleness.
    Neither projection establishes current live Drive state.
    """
    allowed_source_ids = tuple(allowed_source_ids)
    with journal.locked():
        records = journal.records()
        head = source_head(records)
        excluded_ids = {e.get("payload", {}).get("file_id") for e in records if e["kind"] == "EXCLUDED"}
        rows, operations, item_events, run_approvals = [], {}, {}, {}
        planned_orders, planned_sources = {}, set()
        for event in records:
            p = event.get("payload", {})
            if event["kind"] == "DRAFTED" and p.get("plan_ref"):
                plan = _snapshot(journal, p["plan_ref"])
                run_id = p.get("batch_id", p.get("run_id", ""))
                if (p.get("run_id", run_id) != run_id or plan.get("run_id", run_id) != run_id
                        or plan.get("batch_id", run_id) != run_id):
                    raise ValueError("Sealed plan run/batch binding mismatch")
                for order, planned in enumerate(_ordered_plan_operations(plan), 1):
                    key = (run_id, planned["operation_id"])
                    if key in planned_orders and planned_orders[key] != order:
                        raise ValueError("Conflicting sealed operation order")
                    planned_orders[key] = order
                    if planned.get("file_id"):
                        planned_sources.add(planned["file_id"])
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
            op_id = event.get("operation_id")
            if (not op_id or p.get("action") not in {"MOVE", "RENAME"}
                    or kind not in WORKBOOK_STATES | {"RECONCILED_AT_TARGET"}):
                continue
            key = (run_id, op_id)
            if key not in planned_orders:
                raise ValueError("Action state has no sealed plan operation order")
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
                event.get("attempt", 0), planned_orders[key],
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
    basis = _identity_coverage(identity_basis, allowed_source_ids)
    common = {"schema_version": "uqmc.execution.snapshot.v1", "snapshot_id": None,
              "journal_sequence": head["seq"], "journal_tip_sha256": head["hash"],
              "generated_at_utc": generated,
              "coverage": {"basis": "tracked_operations_only", "last_reconciled_at_utc": None,
                           "complete": False, **basis}}
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
            "operation_order": planned_orders[(run_id, op_id)],
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
    for file_id in dict.fromkeys([*item_events, *ancestor_effects, *sorted(planned_sources), *allowed_source_ids]):
        history = item_events.get(file_id, [])
        item = {"source_id": file_id, "source_link": (verified_source_links or {}).get(file_id),
                "reason_codes": [],
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
                if kind == "PREPARED" and last_effect == "NOT_TRACKED" and p.get("before_ref"):
                    # The durable preparation records the verified preimage.
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
            if event["kind"] in {"PREPARED", "VERIFIED_APPLIED", "RECONCILED_AT_TARGET", "FAILED_NOT_APPLIED"}:
                observed = _snapshot(journal, p.get("before_ref") if event["kind"] == "PREPARED" else p.get("observed_ref"))
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
        if file_id in excluded_ids and "WITHDRAWN" not in item["reason_codes"]:
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

    workbook = {**common, "operations": projected_ops, "items": items}
    operation_log = {
        "schema_version": "uqmc.operation-log.transport.v1", "snapshot_id": None,
        "journal_sequence": head["seq"], "journal_tip_sha256": head["hash"],
        "generated_at_utc": generated, "classification": "PRIVATE",
        "sheet_name": "Operation Log", "valueInputOption": "RAW",
        "idempotency_column": "Event ID", "headers": list(HEADERS), "rows": rows,
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
    html = {**common, "operations": public_ops, "items": public_items}
    # A retry after only one surface delivered must not invent a new pair ID.
    # Approval expiry or a changed allowlist changes the projection content and
    # therefore creates a new snapshot even at the same journal watermark.
    nonvolatile = lambda value: {k: v for k, v in value.items() if k not in {
        "snapshot_id", "generated_at_utc", "stale", "freshness"}}
    pair_hash = _hash({"workbook": nonvolatile(workbook), "html": nonvolatile(html),
                       "operation_log": nonvolatile(operation_log)})
    pair_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "uqmc.execution.snapshot.v1:" + pair_hash))
    workbook["snapshot_id"] = html["snapshot_id"] = operation_log["snapshot_id"] = pair_id
    validate_snapshot(workbook)
    validate_snapshot(html)
    return {"workbook": workbook, "html": html, "operation_log": operation_log,
            "diagnostics": {"stale": stale, "freshness": freshness,
                            "identity_basis_origin": "EXPLICIT" if identity_basis else "LOCAL_EXPORT_SELECTION"}}


def delivery_key(destination, payload):
    """Stable across regenerated timestamps and repeated delivery attempts."""
    if destination not in {"workbook", "html", "operation_log"}:
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
    if destination in {"workbook", "html"}:
        validate_snapshot(payload)
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
