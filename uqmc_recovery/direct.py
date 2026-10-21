"""Direct Drive transport loop over the existing frozen plan and action bridge.

No OAuth flow, credentials discovery, changed plan, mutation retry or publication.
AGENT_ASSISTED remains the historical evidence profile; execution_transport and
the sealed tool name explicitly distinguish native API calls from connector calls.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import uuid

from .__main__ import plan_from_json, save_new
from .action_bridge import ActionBridge, DIRECT_API_TOOL
from .agent_profile import agent_snapshot, adjust_descendant_renames
from .engine import ACTION_STATES, SUCCESS, now
from .journal import Journal
from .model import Approval, FOLDER, SafetyError, WITHDRAWN_ID, digest
from .projection import build_projections
from .section_importer import validate_section_authority


TRANSPORT = "DIRECT_GOOGLE_API"
PERMISSION_KEYS = ("type", "role", "emailAddress", "domain", "displayName", "allowFileDiscovery")
ALIASES = {"modified_time": "modifiedTime", "drive_id": "driveId", "version": "version",
           "trashed": "trashed", "owners": "owners", "capabilities": "capabilities",
           "size": "size", "md5_checksum": "md5Checksum", "shortcut_details": "shortcutDetails"}


def canonical_rows(rows):
    return sorted(copy.deepcopy(rows), key=lambda row: json.dumps(row, sort_keys=True))


def ordered_like(rows, prior):
    """Ordering alone is not a change; retain every newly observed row/field."""
    order = {digest(row): index for index, row in enumerate(prior)}
    return sorted(rows, key=lambda row: (order.get(digest(row), len(order)), digest(row)))


def legacy_snapshot(native, template, members=None, scope=None):
    """Explicit native-to-historical projection; complete native data is retained.

    Compare all historically known fields. Additional provider fields are kept in
    preflight.native_* and checked across dispatch/readback, without inventing
    observations in an old sealed plan. Permission additions/losses remain visible.
    """
    raw, permissions = native["file"], native["permissions"]
    out = {"id": raw["id"], "name": raw["name"], "mime_type": raw["mimeType"],
           "parents": raw.get("parents", []), "parents_complete": True}
    if raw.get("trashed") is True:
        raise SafetyError("TRASHED_SOURCE")
    for field, api in ALIASES.items():
        if field in template:
            if template[field] is not None and api not in raw:
                raise SafetyError("DIRECT_KNOWN_METADATA_MISSING")
            out[field] = copy.deepcopy(raw.get(api))
    if raw.get("size") is not None:
        out["size"] = raw["size"]
    # The legacy connector's documented permission projection, not a truncated
    # permission list. Unsupported historical keys fail rather than disappearing.
    prior_permissions = template.get("permissions")
    if prior_permissions is not None:
        keys = set().union(*(set(row) for row in prior_permissions)) if prior_permissions else set(PERMISSION_KEYS)
        if not keys <= set(PERMISSION_KEYS):
            raise SafetyError("DIRECT_UNSUPPORTED_HISTORICAL_PERMISSION_PROFILE")
        projected = [{key: permission.get(key) for key in keys} for permission in permissions]
        out["permissions"] = ordered_like(projected, prior_permissions)
    elif "permissions" in template:
        out["permissions"] = None
    if "access" in template:
        if template["access"] is not None:
            raise SafetyError("DIRECT_UNSUPPORTED_HISTORICAL_ACCESS_PROFILE")
        out["access"] = None
    if out["mime_type"] == FOLDER:
        if members is None:
            raise SafetyError("INCOMPLETE_DESCENDANTS")
        prior = (template.get("subtree") or {}).get("members", [])
        prior_order = {member["id"]: index for index, member in enumerate(prior)}
        members = sorted(members, key=lambda member: (prior_order.get(member["id"], len(prior)), member["id"]))
        out["subtree"] = {"complete": True, "membership_scope": scope, "members": members}
    return agent_snapshot(out)


class DirectRunner:
    def __init__(self, journal, plan, transport, *, controller_id=None):
        self.journal, self.plan, self.transport = journal, plan, transport
        self.controller_id = controller_id or "direct-" + str(uuid.uuid4())
        self.bridge = ActionBridge(journal, "AGENT_ASSISTED", controller_id=self.controller_id,
                                   execution_transport=TRANSPORT)
        self.stop_requested = False
        self._read_times = {}

    def latest(self):
        return {r["operation_id"]: r for r in self.journal.records() if r["kind"] in ACTION_STATES}

    def unresolved(self):
        return [r for r in self.latest().values() if r["kind"] in {"ATTEMPTED", "UNCERTAIN", "BLOCKED_CONFLICT"}]

    def authorization(self):
        rows = [r for r in self.journal.records() if r["kind"] == "HANDOFF_AUTHORIZED"
                and r["payload"].get("proposal_hash") == self.plan.proposal_hash]
        if not rows:
            raise SafetyError("EXISTING_BATCH_AUTHORIZATION_REQUIRED")
        return self.journal.read_evidence(rows[-1]["payload"]["authorization_ref"])

    def dependencies(self, op):
        latest, todo, found = self.latest(), list(op.depends_on), {}
        while todo:
            key = todo.pop()
            if key in found:
                continue
            row = latest.get(key)
            if not row or row["kind"] not in SUCCESS:
                raise SafetyError("HANDOFF_DEPENDENCY_PENDING")
            found[key] = row
            todo.extend(row["payload"].get("depends_on", []))
        return list(found.values())

    def baseline(self, op):
        events = self.dependencies(op)
        same = [r for r in events if r["payload"].get("file_id") == op.file_id]
        before = (self.journal.read_evidence(max(same, key=lambda r: r["seq"])["payload"]["observed_ref"])
                  if same else op.before)
        return adjust_descendant_renames(before, events, self.journal)

    def templates(self, op):
        auth = self.authorization()
        exposure = self.journal.read_evidence(auth["exposure_review_ref"])
        result = {key: self.journal.read_evidence(ref) for key, ref in exposure["access_baselines"].items()}
        for row in sorted(self.dependencies(op), key=lambda r: r["seq"]):
            if row["payload"].get("observed_ref"):
                result[row["payload"]["file_id"]] = self.journal.read_evidence(row["payload"]["observed_ref"])
        return result

    def read_native(self, file_id):
        raw = self.transport.get_file(file_id)
        self._read_times[file_id] = now()
        permissions = self.transport.list_permissions(file_id)
        if raw.get("id") != file_id:
            raise SafetyError("ID_MISMATCH")
        if "permissions" in raw:
            raw["permissions"] = canonical_rows(raw["permissions"])
        return {"file": raw, "permissions": canonical_rows(permissions)}

    def members(self, root_id, descendants=False):
        rows = self.transport.list_children(root_id)
        result = []
        for row in rows:
            member = {"id": row["id"], "name": row["name"], "mime_type": row["mimeType"],
                      "parents": sorted(row.get("parents", []))}
            if root_id not in member["parents"]:
                raise SafetyError("DIRECT_CHILD_PARENT_MISMATCH")
            result.append(member)
            if descendants and row["mimeType"] == FOLDER:
                result.extend(self.members(row["id"], True))
        if len({m["id"] for m in result}) != len(result):
            raise SafetyError("DIRECT_DUPLICATE_OR_CYCLIC_MEMBERSHIP")
        return result

    def capture(self, op, original=None):
        templates = self.templates(op)
        template = original or self.baseline(op)
        source = self.read_native(op.file_id)
        source_members = self.members(op.file_id, True) if source["file"]["mimeType"] == FOLDER else None
        observed = legacy_snapshot(source, template, source_members, "DESCENDANTS")
        before = original or observed
        intended = op.intended(before)
        parents, native_parents = {}, {}

        def proof(value, native):
            # This receipt explicitly identifies a projection of a native API
            # response. It is not represented as an assistant connector result.
            receipt = {"transport": TRANSPORT, "projection": "legacy-known-fields-v1",
                       "structuredContent": value, "provider_response": native}
            return {"kind": "AGENT_CONNECTOR_READ", "observed_at": self._read_times[value["id"]],
                    "snapshot_ref": self.journal.evidence(value), "receipt_ref": self.journal.evidence(receipt)}

        source_proof = proof(observed, source)
        parent_proofs = {}
        for pid in sorted(set(before["parents"]) | set(op.target.get("parents", []))):
            native = self.read_native(pid)
            native_parents[pid] = native
            parents[pid] = legacy_snapshot(native, templates[pid], self.members(pid), "DIRECT_CHILDREN")
            parent_proofs[pid] = proof(parents[pid], native)
        collision_ids = sorted({member["id"] for pid in intended["parents"] for member in parents[pid]["subtree"]["members"]
                                if member["name"] == intended["name"] and member["id"] != op.file_id})
        no_cycle = not set(intended["parents"]) & ({op.file_id} | {m["id"] for m in source_members or []})
        check = {"complete": True, "no_cycle": no_cycle, "parents": parents, "parent_reads": parent_proofs,
                 "source_read": source_proof, "collision_ids": collision_ids, "expected_after": intended, "ancestor_ids": [],
                 "native_source": source, "native_parents": native_parents,
                 "collision_review_ref": self.journal.evidence({"kind": "COLLISION_REVIEW", "proposal_hash": self.plan.proposal_hash,
                     "operation_id": op.operation_id, "complete": True, "target_name": intended["name"],
                     "target_parent_ids": intended["parents"], "collision_ids": collision_ids, "observed_at": now()}),
                 "concurrency": {"mechanism": "SINGLE_CONTROLLER_FRESH_READS", "controller_id": self.controller_id,
                                 "atomic_precondition": False, "race_window_acknowledged": True}}
        if source_members is not None:
            check["membership_ref"] = self.journal.evidence({"kind": "COMPLETE_MEMBERSHIP_READ", "root_id": op.file_id,
                "complete": True, "pagination_complete": True, "membership_scope": "DESCENDANTS",
                "member_ids": [m["id"] for m in source_members], "observed_at": now(), "members_ref": self.journal.evidence(source_members)})
        return observed, check

    def preview(self, *, operation_id=None, limit=1):
        with self.journal.locked():
            self.bridge._frozen(self.plan, self.plan.operations[0].operation_id)
            validate_section_authority(self.journal, self.plan)
            unresolved = self.unresolved()
            if unresolved:
                row = next((r for r in unresolved if r["operation_id"] == operation_id), unresolved[0])
                op = next(o for o in self.plan.operations if o.operation_id == row["operation_id"])
                original = self.journal.read_evidence(row["payload"]["before_ref"])
                observed, check = self.capture(op, original)
                intended = self.journal.read_evidence(row["payload"]["intended_ref"])
                core = lambda value: {key: value[key] for key in ("id", "name", "mime_type", "parents")}
                relation = "AT_TARGET" if core(observed) == core(intended) else "AT_PREIMAGE" if core(observed) == core(original) else "CONFLICT"
                ref = self.journal.evidence({"observed": observed, "preflight": check})
                self.journal.append("RECONCILIATION_READ_RECORDED", operation_id=op.operation_id,
                    payload={"read_ref": ref, "transport": TRANSPORT, "observed_relation": relation,
                             "reason_code": "REQUEST_NOT_PROVEN_TERMINAL", "no_retry": True})
                return {"read_only": True, "blocked": True, "reason_code": "REQUEST_NOT_PROVEN_TERMINAL",
                        "operation_id": op.operation_id, "observed_relation": relation, "evidence_ref": ref,
                        "requires": "Trusted terminal outcome for the original request; a matching current state alone does not release it."}
            results, latest = [], self.latest()
            for op in self.plan.ordered():
                if latest.get(op.operation_id, {}).get("kind") in SUCCESS or (operation_id and op.operation_id != operation_id):
                    continue
                before, check = self.capture(op)
                baseline = self.baseline(op)
                if self.bridge._agent_exact(before) != self.bridge._agent_exact(self.bridge._with_observed_unknown_size(baseline, before)):
                    raise SafetyError("HANDOFF_BASELINE_DRIFT")
                if check["collision_ids"] or not check["no_cycle"]:
                    raise SafetyError("DIRECT_PREFLIGHT_CONFLICT")
                ref = self.journal.evidence({"operation_id": op.operation_id, "before": before, "preflight": check})
                results.append({"operation_id": op.operation_id, "state": "READ_ONLY_OBSERVED", "evidence_ref": ref})
                if len(results) >= limit:
                    break
            return {"read_only": True, "blocked": False, "results": results, "execution_approved": False}

    def _enable(self):
        """An explicit local apply invocation rebinds existing human authority."""
        auth = copy.deepcopy(self.authorization())
        clock = datetime.now(timezone.utc)
        approval = Approval("direct-" + str(uuid.uuid4()), self.plan.run_id, self.plan.batch_id,
            self.plan.proposal_revision, self.plan.proposal_hash, tuple(o.operation_id for o in self.plan.operations),
            auth["actor"], clock.isoformat(), (clock + timedelta(hours=4)).isoformat(), "AGENT_ASSISTED")
        auth["approval_hash"] = digest(approval.body())
        proof = self.journal.evidence({"kind": "TRUSTED_CONTROLLER_REVIEW", "text": "Explicit local --apply invocation with exact frozen plan hash; existing section authority retained.",
            "execution_transport": TRANSPORT, "proposal_hash": self.plan.proposal_hash, "existing_user_provenance_ref": auth["provenance_ref"]})
        enable = {"kind": "DISPATCH_ENABLE_REVIEW", "controller_id": self.controller_id,
            "execution_transport": TRANSPORT, "proposal_hash": self.plan.proposal_hash, "approval_id": approval.approval_id,
            "authorization_hash": digest(auth), "manifest_verified": True, "reviewer": "local operator apply invocation",
            "reviewed_at": clock.isoformat(), "expires_at": approval.expires_at, "provenance_ref": proof}
        self.bridge.authorize_agent_dispatch(self.plan, approval, auth, enable)
        return approval

    @staticmethod
    def native_postcheck(op, before, after):
        source_before, source_after = before["native_source"], after["native_source"]
        for key in ("id", "mimeType", "size", "md5Checksum", "owners", "driveId", "shortcutDetails", "trashed"):
            if key in source_before["file"] and source_after["file"].get(key) != source_before["file"][key]:
                raise SafetyError("DIRECT_NATIVE_SOURCE_DRIFT")
        if op.action == "RENAME" and source_before["permissions"] != source_after["permissions"]:
            raise SafetyError("DIRECT_NATIVE_PERMISSION_DRIFT")
        if op.action == "MOVE":
            # Inherited access may change under the existing scoped approval;
            # a known direct grant must not silently disappear or change role.
            current = {p.get("id"): p for p in source_after["permissions"]}
            for permission in source_before["permissions"]:
                if any(detail.get("inherited") is False for detail in permission.get("permissionDetails", [])):
                    observed = current.get(permission.get("id"), {})
                    if any(observed.get(key) != permission.get(key) for key in ("id", "type", "role", "emailAddress", "domain", "allowFileDiscovery")):
                        raise SafetyError("DIRECT_NATIVE_DIRECT_GRANT_DRIFT")
        for pid, prior in before["native_parents"].items():
            current = after["native_parents"][pid]
            if prior["permissions"] != current["permissions"]:
                raise SafetyError("DIRECT_NATIVE_PARENT_PERMISSION_DRIFT")
            for key in ("id", "name", "mimeType", "parents", "owners", "driveId", "trashed"):
                if key in prior["file"] and current["file"].get(key) != prior["file"][key]:
                    raise SafetyError("DIRECT_NATIVE_PARENT_DRIFT")

    def apply(self, *, confirm_plan_sha256, max_operations=1, progress=None):
        if confirm_plan_sha256 != self.plan.proposal_hash:
            raise SafetyError("EXACT_PLAN_CONFIRMATION_REQUIRED")
        if not self.transport.apply_enabled:
            raise SafetyError("DIRECT_READ_ONLY_TRANSPORT")
        with self.journal.locked():
            if self.unresolved():
                raise SafetyError("UNRESOLVED_PRIOR_REQUEST_RECONCILE_FIRST")
            self.bridge._frozen(self.plan, self.plan.operations[0].operation_id)
            backup_root = self.journal.root.parent / "backups"
            backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.journal.backup(backup_root / ("direct-before-" + str(uuid.uuid4())))
            approval = self._enable()
            completed = 0
            try:
                for op in self.plan.ordered():
                    state = self.latest().get(op.operation_id, {}).get("kind")
                    if state in SUCCESS:
                        continue
                    if state is not None:
                        raise SafetyError("EXISTING_ATTEMPT_OR_PREPARATION_REQUIRES_RECONCILIATION")
                    if self.stop_requested or completed >= max_operations:
                        break
                    before, check = self.capture(op)
                    envelope = self.bridge.prepare_action(self.plan, op.operation_id, approval, before, check)
                    fresh, final_check = self.capture(op)
                    if self.stop_requested:
                        break
                    ticket = self.bridge.mark_attempt(envelope["envelope_hash"], fresh, final_check)
                    if ticket["tool"] != DIRECT_API_TOOL or ticket["args"] != envelope["args"]:
                        raise SafetyError("DIRECT_TICKET_MISMATCH")
                    # Exactly one invocation, never a provider exactly-once claim.
                    raw, error = None, None
                    try:
                        result = self.transport.update_file(ticket["args"])
                        raw = {"transport": TRANSPORT, "structuredContent": {"id": result.get("id")}, "provider_response": result}
                    except Exception as exc:
                        error = type(exc).__name__
                        raw = {"transport": TRANSPORT, "isError": True, "error_code": getattr(exc, "code", error), "outcome": "UNKNOWN"}
                    completed_at = now()
                    response_ref = self.bridge.ingest_result(ticket["envelope_hash"], ticket["attempt_key"], raw)
                    terminal = {"kind": "AGENT_UNRESOLVED", "mode": "AGENT_ASSISTED", "terminal": False,
                                "attempt_key": ticket["attempt_key"], "envelope_hash": ticket["envelope_hash"]}
                    if not error and raw["structuredContent"]["id"] == op.file_id:
                        completion = self.journal.evidence({"kind": "TRUSTED_TOOL_COMPLETION", "tool": DIRECT_API_TOOL,
                            "tool_call_id": "local-api-invocation:" + ticket["attempt_key"], "outcome": "completed_success",
                            "args_hash": digest(ticket["args"]), "raw_response_hash": digest(raw), "completed_at": completed_at})
                        terminal.update(kind="AGENT_TOOL_COMPLETED", terminal=True, response_ref=response_ref, provenance_ref=completion)
                    observed, postflight = self.capture(op, before)
                    try:
                        self.native_postcheck(op, final_check, postflight)
                    except SafetyError as exc:
                        prepared = [r for r in self.journal.records() if r["kind"] == "PREPARED" and r["operation_id"] == op.operation_id][-1]
                        self.journal.append("BLOCKED_CONFLICT", operation_id=op.operation_id, payload=prepared["payload"] |
                            {"reason_code": str(exc), "observed_ref": self.journal.evidence(observed), "postflight_ref": self.journal.evidence(postflight)})
                        raise
                    record = self.bridge.reconcile_action(ticket["envelope_hash"], observed, postflight, terminal)
                    if record["kind"] != "RECONCILED_AT_TARGET":
                        raise SafetyError("DIRECT_ACTION_REQUIRES_RECONCILIATION")
                    completed += 1
                    if progress:
                        progress({"completed_this_invocation": completed, "operation_id": op.operation_id, "journal_sequence": record["seq"]})
                return {"completed_this_invocation": completed, "stopped": self.stop_requested}
            finally:
                self.bridge.disable_agent_dispatch("DIRECT_INVOCATION_ENDED")
                self.journal.backup(backup_root / ("direct-after-" + str(uuid.uuid4())))


def export_status(journal, identity_basis_path, destination):
    basis = json.loads(Path(identity_basis_path).read_text())
    values = build_projections(journal, identity_basis=basis, allowed_source_ids=basis["source_ids"])
    prefix = str(uuid.uuid4())
    return {key: save_new(Path(destination) / (prefix + "-" + key + ".json"), value) for key, value in values.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "preview", "reconcile", "apply"))
    parser.add_argument("--store", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--token", help="Explicit protected authorized-user JSON; no OAuth flow is started")
    parser.add_argument("--operation-id")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--apply", action="store_true", help="Required separately from the apply subcommand")
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--identity-basis")
    parser.add_argument("--export-dir")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    if bool(args.identity_basis) != bool(args.export_dir):
        parser.error("--identity-basis and --export-dir must be supplied together")
    plan = plan_from_json(json.loads(Path(args.plan).read_text()))
    journal = Journal(args.store)
    try:
        if args.command == "status":
            latest = {r["operation_id"]: r for r in journal.records() if r["kind"] in ACTION_STATES}
            completed = [op for op in plan.operations if latest.get(op.operation_id, {}).get("kind") in SUCCESS]
            print(json.dumps({"plan_sha256": plan.proposal_hash, "completed": len(completed), "remaining": len(plan.operations)-len(completed),
                              "unresolved": [key for key, row in latest.items() if row["kind"] in {"ATTEMPTED", "UNCERTAIN", "BLOCKED_CONFLICT"}],
                              "network_calls": 0, "mutations": 0}))
            return 0
        if args.command == "apply" and (not args.apply or args.confirm_plan_sha256 != plan.proposal_hash):
            raise SafetyError("EXPLICIT_APPLY_AND_EXACT_PLAN_HASH_REQUIRED")
        if args.command != "apply" and args.apply:
            raise SafetyError("PREVIEW_CANNOT_APPLY")
        if not args.token:
            raise SafetyError("EXPLICIT_GOOGLE_AUTHORIZATION_SETUP_REQUIRED")
        from .direct_transport import GoogleDriveTransport
        excluded = {WITHDRAWN_ID} | set(plan.excluded_ids) | {r["payload"]["file_id"] for r in journal.records() if r["kind"] == "EXCLUDED"}
        allowed = {op.file_id for op in plan.operations}
        for op in plan.operations:
            allowed |= set(op.before["parents"]) | set(op.target.get("parents", []))
            allowed |= {m["id"] for m in (op.before.get("subtree") or {}).get("members", [])}
        transport = GoogleDriveTransport.from_authorized_user_file(args.token, apply=args.command == "apply", allowed_ids=allowed, excluded_ids=excluded)
        runner = DirectRunner(journal, plan, transport)
        def stop(signum, frame):
            runner.stop_requested = True
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        if args.command == "apply":
            result = runner.apply(confirm_plan_sha256=args.confirm_plan_sha256, max_operations=args.limit,
                                  progress=lambda result: print(json.dumps(result), flush=True))
        else:
            result = runner.preview(operation_id=args.operation_id, limit=args.limit)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        # Never stringify HTTP/auth errors into chat, exports or logs.
        print(json.dumps({"blocked": True, "reason_code": str(exc) if isinstance(exc, SafetyError) else getattr(exc, "code", type(exc).__name__)}))
        return 2
    finally:
        if args.identity_basis:
            try:
                print(json.dumps({"status_exports": export_status(journal, args.identity_basis, args.export_dir), "published": False}))
            except Exception as exc:
                print(json.dumps({"status_export_failed": type(exc).__name__, "actions_not_retried": True}))


if __name__ == "__main__":
    raise SystemExit(main())
