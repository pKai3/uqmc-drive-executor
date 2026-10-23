"""Fail-closed simulation executor with durable intent and reconciliation.

Only MockAdapter can dispatch. No live integration, credential handling or network.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime, timezone

from .model import (Approval, FOLDER, Operation, Plan, SafetyError, WITHDRAWN_ID, digest, semantic,
                    snapshot, validate_extension_evidence, validate_rollback_links)

SUCCESS = {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET", "SKIPPED"}
ACTION_STATES = SUCCESS | {"PREPARED", "ATTEMPTED", "UNCERTAIN", "FAILED_NOT_APPLIED", "BLOCKED_CONFLICT"}


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parsed(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise SafetyError("TIMEZONE_REQUIRED")
    return result


class Engine:
    def __init__(self, journal, adapter, fault=None, excluded_ids=()):
        self.journal = journal
        self.adapter = adapter
        self.fault = fault or (lambda point: None)
        for file_id in excluded_ids:
            self.exclude(file_id)

    def _emit(self, kind, plan, op=None, attempt=0, **payload):
        base = {"run_id": plan.run_id, "batch_id": plan.batch_id,
                "proposal_revision": plan.proposal_revision, "proposal_hash": plan.proposal_hash}
        if op:
            base.update(action=op.action, direction=op.direction, file_id=op.file_id,
                        depends_on=list(op.depends_on), proposal_refs=list(op.proposal_refs),
                        original_operation_id=op.original_operation_id)
        base.update(payload)
        self.fault("before_" + kind)
        record = self.journal.append(kind, operation_id=op.operation_id if op else None,
                                     attempt=attempt, payload=base)
        self.fault("after_" + kind)
        return record

    def events(self, operation_id):
        return [r for r in self.journal.records() if r["operation_id"] == operation_id and r["kind"] in ACTION_STATES]

    def latest(self, operation_id):
        events = self.events(operation_id)
        return events[-1] if events else None

    def exclusions(self):
        return {WITHDRAWN_ID} | {r["payload"]["file_id"] for r in self.journal.records() if r["kind"] == "EXCLUDED"}

    def exclude(self, file_id, reason="WITHDRAWN"):
        with self.journal.locked():
            if file_id not in self.exclusions():
                self.journal.append("EXCLUDED", payload={"file_id": file_id, "reason_code": reason})

    def revoke(self, approval_id):
        self.journal.append("REVOKED", payload={"approval_id": approval_id})

    def _guard_plan(self, plan):
        plan.validate()
        if plan.run_id != plan.batch_id:
            raise SafetyError("RUN_MUST_EQUAL_BATCH")
        excluded = self.exclusions() | set(plan.excluded_ids)
        excluded_refs = {r["payload"]["proposal_ref"] for r in self.journal.records() if r["kind"] == "PROPOSAL_EXCLUDED"}
        for op in plan.operations:
            if set(op.proposal_refs) & excluded_refs:
                raise SafetyError("PROPOSAL_EXCLUDED")
            scope = {op.file_id} | set(op.before["parents"]) | set(op.target.get("parents", []))
            scope |= {m["id"] for m in op.before["subtree"].get("members", [])}
            if scope & excluded:
                raise SafetyError("EXCLUDED")
            # Fence every earlier indeterminate request, including overlapping folder scope.
            latest_by_id = {}
            for record in self.journal.records():
                if record["kind"] in ACTION_STATES and record["operation_id"]:
                    latest_by_id[record["operation_id"]] = record
            for previous_id, record in latest_by_id.items():
                reserved = record["kind"] in {"ATTEMPTED", "UNCERTAIN"} or (
                    record["kind"] == "PREPARED" and record["payload"].get("bridge"))
                if previous_id in {item.operation_id for item in plan.operations} or not reserved:
                    continue
                previous_scope = {record["payload"]["file_id"]} | set(record["payload"].get("scope_ids", []))
                ref = record["payload"].get("before_ref")
                if ref:
                    old = self.journal.read_evidence(ref)
                    previous_scope |= {m["id"] for m in old["subtree"].get("members", [])}
                if previous_scope & scope:
                    raise SafetyError("UNRESOLVED_PRIOR_REQUEST")
        return excluded

    def register(self, plan):
        with self.journal.locked():
            # Tombstones survive rejected or superseded proposal revisions.
            for item in plan.excluded_ids:
                self.exclude(item)
            self._guard_plan(plan)
            validate_rollback_links(self.journal, plan)
            for operation in plan.operations:
                validate_extension_evidence(self.journal, operation)
            existing = [r for r in self.journal.records() if r["kind"] == "DRAFTED" and r["payload"]["run_id"] == plan.run_id]
            if existing:
                if existing[-1]["payload"]["proposal_hash"] != plan.proposal_hash:
                    raise SafetyError("BATCH_ALREADY_FROZEN")
                return
            taken = {r["operation_id"] for r in self.journal.records() if r["operation_id"]}
            for record in self.journal.records():
                if record["kind"] == "DRAFTED":
                    taken |= {item["operation_id"] for item in self.journal.read_evidence(record["payload"]["plan_ref"])["operations"]}
            if taken & {x.operation_id for x in plan.operations}:
                raise SafetyError("OPERATION_ID_REUSED")
            ref = self.journal.evidence(plan.body())
            self._emit("DRAFTED", plan, plan_ref=ref)

    def _require_frozen(self, plan, op=None):
        records = [r for r in self.journal.records() if r["kind"] == "DRAFTED" and r["payload"]["run_id"] == plan.run_id]
        if not records or records[-1]["payload"]["proposal_hash"] != plan.proposal_hash:
            raise SafetyError("FROZEN_PLAN_MISMATCH")
        if op is not None and op not in plan.operations:
            raise SafetyError("OPERATION_NOT_IN_FROZEN_PLAN")

    def _approval(self, plan, approval):
        if approval.mode != "TEST_ONLY" or not getattr(self.adapter, "is_mock", False):
            raise SafetyError("LIVE_EXECUTION_DISABLED")
        fields = ("run_id", "batch_id", "proposal_revision", "proposal_hash")
        if any(getattr(plan, key) != getattr(approval, key) for key in fields):
            raise SafetyError("STALE_APPROVAL")
        if set(approval.operation_ids) != {op.operation_id for op in plan.operations} or len(set(approval.operation_ids)) != len(approval.operation_ids):
            raise SafetyError("APPROVAL_SCOPE")
        if not approval.actor or not approval.approval_id:
            raise SafetyError("MISSING_APPROVAL_IDENTITY")
        current = datetime.now(timezone.utc)
        if not parsed(approval.approved_at) <= current < parsed(approval.expires_at):
            raise SafetyError("APPROVAL_EXPIRED_OR_FUTURE")
        if any(r["kind"] == "REVOKED" and r["payload"]["approval_id"] == approval.approval_id for r in self.journal.records()):
            raise SafetyError("APPROVAL_REVOKED")
        prior = [r for r in self.journal.records() if r["kind"] == "APPROVED" and r["payload"]["approval_id"] == approval.approval_id]
        ref = self.journal.evidence(approval.body())
        if prior and prior[-1]["payload"]["approval_ref"] != ref:
            raise SafetyError("APPROVAL_ID_REUSED")
        if not prior:
            self._emit("APPROVED", plan, approval_id=approval.approval_id, approval_ref=ref,
                       actor=approval.actor, mode=approval.mode)
        return ref

    def _read(self, file_id):
        if file_id in self.exclusions():
            raise SafetyError("EXCLUDED")
        state = snapshot(self.adapter.get(file_id))
        if state["id"] != file_id:
            raise SafetyError("ID_MISMATCH")
        return state

    def _baseline(self, op):
        # Only known, verified same-file predecessors can refresh version metadata.
        predecessors, todo, visited = [], list(op.depends_on), set()
        while todo:
            dep = todo.pop()
            if dep in visited:
                continue
            visited.add(dep)
            record = self.latest(dep)
            if record:
                predecessors.append(record)
                todo.extend(record["payload"].get("depends_on", []))
        same_file = [p for p in predecessors if p and p["kind"] in SUCCESS and p["payload"].get("file_id") == op.file_id]
        if same_file:
            newest = max(same_file, key=lambda x: x["seq"])
            observed = self.journal.read_evidence(newest["payload"]["observed_ref"])
            if semantic(observed) != semantic(op.before):
                raise SafetyError("DEPENDENCY_BASELINE_MISMATCH")
            return observed
        return snapshot(op.before)

    def _preflight(self, op, before, excluded, approval=None):
        from .shortcut_policy import guard_shortcut_mutation
        guard_shortcut_mutation(before, op.target)
        check = self.adapter.preflight(op, before, excluded)
        if not check.get("complete") or not check.get("rollback_possible"):
            raise SafetyError("INCOMPLETE_PREFLIGHT")
        # Drive permits identical names. Only exact, frozen, approved peer IDs
        # may already occupy the intended parent/name; no overwrite is issued.
        if set(check.get("collision_ids", [])) - set(op.allowed_collision_ids):
            raise SafetyError("NAME_COLLISION")
        if not check.get("no_cycle"):
            raise SafetyError("FOLDER_CYCLE")
        if set(check.get("descendant_ids", [])) & excluded:
            raise SafetyError("EXCLUDED_DESCENDANT")
        if before["mime_type"] == FOLDER and not check.get("descendants_complete"):
            raise SafetyError("INCOMPLETE_DESCENDANTS")
        if not before["capabilities"].get("can_move" if op.action == "MOVE" else "can_rename"):
            raise SafetyError("MISSING_CAPABILITY")
        required_parents = set(before["parents"]) | set(op.target.get("parents", []))
        if required_parents != set(check.get("parents", {})):
            raise SafetyError("INCOMPLETE_PARENTS")
        for parent in check["parents"].values():
            parent = snapshot(parent)
            if parent["id"] in excluded or parent["mime_type"] != FOLDER or not parent["capabilities"].get("can_add_children"):
                raise SafetyError("INVALID_PARENT")
        expected = snapshot(check["expected_after"])
        minimal = op.intended(before)
        for key in minimal:
            if key not in {"permissions", "access", "subtree"} and expected[key] != minimal[key]:
                raise SafetyError("PREFLIGHT_SCOPE_MISMATCH")
        access_hash = digest({"before": {k: before[k] for k in ("permissions", "access", "subtree")},
                              "after": {k: expected[k] for k in ("permissions", "access", "subtree")}})
        access_changed = any(before[k] != expected[k] for k in ("permissions", "access", "subtree"))
        if access_changed and (approval is None or approval.access_change_hashes.get(op.operation_id) != access_hash):
            raise SafetyError("ACCESS_CHANGE_REQUIRES_EXPLICIT_APPROVAL")
        return check, expected

    def dry_run(self, plan):
        """Read-only remote work; writes evidence locally, creates no approval/action events."""
        with self.journal.locked():
            excluded = self._guard_plan(plan)
            results = []
            for op in plan.ordered():
                try:
                    before = self._read(op.file_id)
                    if before != snapshot(op.before):
                        raise SafetyError("BASELINE_DRIFT")
                    check, intended = self._preflight(op, before, excluded)
                    results.append({"operation_id": op.operation_id, "state": "READ_ONLY_READY", "evidence_ref": self.journal.evidence(check)})
                except Exception as error:
                    results.append({"operation_id": op.operation_id, "state": "BLOCKED", "reason_code": str(error) if isinstance(error, SafetyError) else "READ_UNAVAILABLE"})
            ref = self.journal.evidence({"at": now(), "plan_hash": plan.proposal_hash, "dry_run": True, "results": results})
            return {"dry_run": True, "live_execution_enabled": False, "evidence_ref": ref, "results": results}

    def _attempt_data(self, op):
        prepared = [r for r in self.events(op.operation_id) if r["kind"] == "PREPARED"]
        if not prepared:
            raise SafetyError("MISSING_PREIMAGE")
        record = prepared[-1]
        return record, self.journal.read_evidence(record["payload"]["before_ref"]), self.journal.read_evidence(record["payload"]["intended_ref"])

    def reconcile(self, plan, op):
        with self.journal.locked():
            self._require_frozen(plan, op)
            current = self.latest(op.operation_id)
            if current and current["payload"].get("bridge"):
                raise SafetyError("HANDOFF_OWNS_OPERATION")
            self._guard_plan(plan)
            latest = self.latest(op.operation_id)
            if not latest or latest["kind"] not in {"ATTEMPTED", "UNCERTAIN"}:
                return latest
            prepared, before, intended = self._attempt_data(op)
            payload = dict(prepared["payload"])
            for k in ("run_id", "batch_id", "proposal_revision", "proposal_hash", "action", "direction", "file_id", "depends_on", "proposal_refs", "original_operation_id"):
                payload.pop(k, None)
            key = payload["attempt_key"]
            try:
                observed = self._read(op.file_id)
                payload["observed_ref"] = self.journal.evidence(observed)
                self.fault("after_reconcile_read")
                # A request can still complete after an immediate read of the preimage.
                if not self.adapter.terminal(key):
                    kind, reason = "UNCERTAIN", "REQUEST_NOT_PROVEN_TERMINAL"
                elif semantic(observed) == semantic(intended):
                    preflight = self.adapter.preflight(op, before, self.exclusions() | set(plan.excluded_ids))
                    payload["postflight_ref"] = self.journal.evidence(preflight)
                    if preflight != self.journal.read_evidence(payload["preflight_ref"]):
                        kind, reason = "BLOCKED_CONFLICT", "RECONCILIATION_SCOPE_DRIFT"
                    else:
                        kind, reason = "RECONCILED_AT_TARGET", "OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN"
                elif observed == before:
                    kind, reason = "FAILED_NOT_APPLIED", "TERMINAL_AND_PREIMAGE_VERIFIED"
                else:
                    kind, reason = "BLOCKED_CONFLICT", "RECONCILIATION_DRIFT"
            except SafetyError:
                kind, reason = "BLOCKED_CONFLICT", "RECONCILIATION_METADATA_BLOCKED"
            except Exception:
                kind, reason = "UNCERTAIN", "READ_UNAVAILABLE"
            payload["reason_code"] = reason
            payload["reconciled_at"] = now()
            return self._emit(kind, plan, op, prepared["attempt"], **payload)

    def run(self, plan, approval):
        """Simulation only. Entire batch holds one process lock, including dispatch/readback."""
        with self.journal.locked():
            from .adapters import MockAdapter
            if type(self.adapter) is not MockAdapter:
                raise SafetyError("LIVE_EXECUTION_DISABLED")
            for operation in plan.operations:
                current = self.latest(operation.operation_id)
                if current and current["payload"].get("bridge"):
                    raise SafetyError("HANDOFF_OWNS_OPERATION")
            if (self.journal.root / "snapshot-manifest.json").exists():
                raise SafetyError("RESTORED_STORE_REQUIRES_RECONCILIATION")
            self.register(plan)
            self._approval(plan, approval)
            excluded = self._guard_plan(plan)
            for op in plan.ordered():
                current = self.latest(op.operation_id)
                if current and current["kind"] in {"ATTEMPTED", "UNCERTAIN"}:
                    current = self.reconcile(plan, op)
                if current and current["kind"] in SUCCESS:
                    continue
                # Safe retries require a NEW batch/approval; same batch never silently retries.
                if current and current["kind"] in {"UNCERTAIN", "BLOCKED_CONFLICT", "FAILED_NOT_APPLIED"}:
                    continue
                dep_states = [self.latest(d) for d in op.depends_on]
                if any(not state or state["kind"] not in SUCCESS for state in dep_states):
                    # Do not turn an unattempted dependency wait into a terminal operation failure.
                    continue
                self._approval(plan, approval)
                excluded = self._guard_plan(plan)
                try:
                    before = self._baseline(op)
                    observed = self._read(op.file_id)
                    if observed != before:
                        raise SafetyError("BASELINE_DRIFT")
                    check, intended = self._preflight(op, before, excluded, approval)
                    before_ref = self.journal.evidence(before)
                    intended_ref = self.journal.evidence(intended)
                    check_ref = self.journal.evidence(check)
                    if semantic(before) == semantic(intended):
                        self._emit("SKIPPED", plan, op, 0, before_ref=before_ref,
                                   intended_ref=intended_ref, observed_ref=before_ref,
                                   actor=approval.actor, reason_code="ALREADY_AT_APPROVED_STATE")
                        continue
                    # PREPARED on restart was never dispatched. Recheck and reuse its attempt ID.
                    attempt = current["attempt"] if current and current["kind"] == "PREPARED" else 1
                    key = f"{plan.run_id}:{op.operation_id}:{attempt}"
                    payload = dict(before_ref=before_ref, intended_ref=intended_ref, preflight_ref=check_ref,
                                   permission_ref=before_ref, approval_id=approval.approval_id,
                                   actor=approval.actor, attempt_key=key)
                    self._emit("PREPARED", plan, op, attempt, **payload)
                    self.fault("before_final_read")
                    if self._read(op.file_id) != before or self.adapter.preflight(op, before, excluded) != check:
                        raise SafetyError("PRE_DISPATCH_DRIFT")
                    self.fault("after_final_read")
                    self._approval(plan, approval)
                    self._emit("ATTEMPTED", plan, op, attempt, **payload)
                except SafetyError as error:
                    self._emit("BLOCKED_CONFLICT", plan, op, current["attempt"] if current else 0,
                               actor=approval.actor, reason_code=str(error))
                    continue
                # After ATTEMPTED, all failures remain uncertain until a read plus terminal proof.
                try:
                    self.fault("before_remote_apply")
                    response = self.adapter.apply(op, before, check, key)
                    self.fault("after_remote_apply")
                    payload["response_ref"] = self.journal.evidence(response)
                    self.fault("after_response_evidence")
                    observed = self._read(op.file_id)
                    self.fault("after_readback")
                    payload["observed_ref"] = self.journal.evidence(observed)
                    postflight = self.adapter.preflight(op, before, excluded)
                    payload["postflight_ref"] = self.journal.evidence(postflight)
                    self.fault("after_postflight")
                    if semantic(observed) != semantic(intended) or postflight != check:
                        self._emit("BLOCKED_CONFLICT", plan, op, attempt, **payload, reason_code="POST_ACTION_DRIFT")
                    elif not self.adapter.terminal(key):
                        self._emit("UNCERTAIN", plan, op, attempt, **payload, reason_code="REQUEST_NOT_PROVEN_TERMINAL")
                    else:
                        self._emit("VERIFIED_APPLIED", plan, op, attempt, **payload, reason_code="EXACT_POSTIMAGE_VERIFIED")
                except Exception:
                    self._emit("UNCERTAIN", plan, op, attempt, **payload, reason_code="DISPATCH_OR_READBACK_FAILED")
            return {op.operation_id: (self.latest(op.operation_id) or {"kind": "NOT_ATTEMPTED"})["kind"] for op in plan.operations}

    def rollback_plan(self, forward, selected_ids, batch_id):
        """Construct compensation only; caller must separately approve the returned plan."""
        with self.journal.locked():
            self._require_frozen(forward)
            self._guard_plan(forward)
            selected = set(selected_ids)
            if not selected <= {op.operation_id for op in forward.operations}:
                raise SafetyError("UNKNOWN_ROLLBACK_SELECTION")
            states = {op.operation_id: self.latest(op.operation_id) for op in forward.operations}
            if any(s and s["kind"] in {"ATTEMPTED", "UNCERTAIN"} for s in states.values()):
                raise SafetyError("RECONCILE_BEFORE_ROLLBACK")
            reversed_ids = {r["payload"].get("original_operation_id") for r in self.journal.records()
                            if r["kind"] in SUCCESS and r["payload"].get("direction") == "ROLLBACK"}
            active = {k for k, s in states.items() if s and s["kind"] in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"} and k not in reversed_ids}
            selected &= active
            closure = {}
            for op in forward.ordered():
                closure[op.operation_id] = set(op.depends_on)
                for dep in op.depends_on:
                    closure[op.operation_id] |= closure[dep]
                if op.operation_id in active - selected and closure[op.operation_id] & selected:
                    raise SafetyError("ROLLBACK_HAS_ACTIVE_DEPENDENTS")
            operations, simulated = [], {}
            for op in reversed(forward.ordered()):
                if op.operation_id not in selected:
                    continue
                event = states[op.operation_id]
                before = self.journal.read_evidence(event["payload"]["before_ref"])
                after = self.journal.read_evidence(event["payload"]["observed_ref"])
                baseline = simulated.get(op.file_id, after)
                if semantic(baseline) != semantic(after):
                    raise SafetyError("ROLLBACK_CHAIN_DRIFT")
                target = {"name": before["name"]} if op.action == "RENAME" else {"parents": before["parents"]}
                # Serial reverse topological order also preserves actual issue order for independents.
                deps = (operations[-1].operation_id,) if operations else ()
                inverse = Operation(f"{batch_id}:undo:{op.operation_id}", op.file_id, op.action,
                                    copy.deepcopy(baseline), target, deps, op.proposal_refs,
                                    "ROLLBACK", op.operation_id)
                operations.append(inverse)
                simulated[op.file_id] = copy.deepcopy(before)
                simulated[op.file_id]["version"] = baseline["version"]
                simulated[op.file_id]["modified_time"] = baseline["modified_time"]
            result = Plan(batch_id, batch_id, "rollback:" + forward.proposal_revision,
                          tuple(operations), forward.excluded_ids)
            result.validate()
            return result
