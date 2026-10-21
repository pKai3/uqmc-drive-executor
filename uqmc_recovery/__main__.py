"""Local CLI. No command sends Drive mutations or publishes projections."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .adapters import MockAdapter, fixture_state
from .engine import Engine
from .journal import Journal
from .model import Approval, FOLDER, Operation, Plan, SafetyError, digest
from .projection import build_projections


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        # Preserve uncertain/partial output for diagnosis; never overwrite it.
        raise
    return str(path.absolute())


def plan_from_json(value):
    """Parse a frozen manifest without adding defaults or broadening its scope."""
    body = dict(value)
    body["operations"] = tuple(Operation(**operation) for operation in body["operations"])
    return Plan(**body)


def require_policy_profile(journal, plan, policy):
    """The configured task cannot bypass its section method by using raw JSON."""
    profile = policy.get("required_method_profile")
    if not profile:
        return
    from .section_importer import validate_section_authority
    from .model import validate_rollback_links
    with journal.locked():
        if all(op.direction == "FORWARD" for op in plan.operations):
            authority = validate_section_authority(journal, plan)
            if not authority:
                raise SafetyError("REVIEWED_SECTION_ORIGIN_REQUIRED")
            origins = [journal.read_evidence(authority["section_manifest_ref"])]
        elif plan.operations and all(op.direction == "ROLLBACK" for op in plan.operations):
            # Cancellation must stop forwards while permitting separately
            # approved recovery. Verify historical provenance, not old consent.
            validate_rollback_links(journal, plan)
            records = journal.records()
            original_hashes = set()
            for op in plan.operations:
                successes = [r for r in records if r["operation_id"] == op.original_operation_id
                             and r["kind"] in {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"}]
                original_hashes.add(successes[-1]["payload"]["proposal_hash"])
            origins = []
            for original_hash in original_hashes:
                imported = [r for r in records if r["kind"] == "SECTION_IMPORTED"
                            and r["payload"].get("proposal_hash") == original_hash]
                if not imported:
                    raise SafetyError("REVIEWED_SECTION_ORIGIN_REQUIRED")
                evidence = journal.read_evidence(imported[-1]["payload"]["manifest_ref"])
                if digest(evidence.get("plan")) != original_hash:
                    raise SafetyError("SECTION_PLAN_ORIGIN_MISMATCH")
                origins.append(evidence)
        else:
            raise SafetyError("INVALID_TASK_DIRECTION_SCOPE")
        for evidence in origins:
            manifest = evidence.get("manifest", {})
            if (manifest.get("section_approval_validated") is not True
                    or manifest.get("authorization_scope", {}).get("method_profile") != profile):
                raise SafetyError("METHOD_PROFILE_MISMATCH")


def demo(root):
    """Synthetic TEST_ONLY approvals and in-memory remote state; never a pilot."""
    store = Journal(root)
    states = {"origin": fixture_state("origin", "Original", FOLDER, ()),
              "destination": fixture_state("destination", "Destination", FOLDER, ()),
              "fixture-file": fixture_state("fixture-file", "Report.pdf")}
    adapter = MockAdapter(states)
    move = Operation("demo-move", "fixture-file", "MOVE", states["fixture-file"], {"parents": ["destination"]}, proposal_refs=("MOVE-fixture",))
    rename = Operation("demo-rename", "fixture-file", "RENAME", move.intended(), {"name": "Reviewed Report.pdf"}, (move.operation_id,), ("RENAME-fixture",))
    plan = Plan("demo-forward", "demo-forward", "fixture-revision-1", (move, rename))

    def approve(selected):
        current = datetime.now(timezone.utc)
        return Approval(str(uuid4()), selected.run_id, selected.batch_id, selected.proposal_revision,
                        selected.proposal_hash, tuple(o.operation_id for o in selected.operations),
                        "fixture-actor", current.isoformat(), (current + timedelta(hours=1)).isoformat())

    count = 0
    def fault(point):
        nonlocal count
        if point == "before_remote_apply":
            count += 1
            if count == 2:
                adapter.failure = "after_apply"
    engine = Engine(store, adapter, fault=fault)
    approval = approve(plan)
    partial = engine.run(plan, approval)
    resumed = engine.run(plan, approval)
    compensation = engine.rollback_plan(plan, [o.operation_id for o in plan.operations], "demo-rollback")
    rolled_back = engine.run(compensation, approve(compensation))
    calls = len(adapter.calls)
    repeated = engine.run(compensation, approve(compensation))
    if len(adapter.calls) != calls:
        raise AssertionError("Repeat rollback dispatched another mutation")
    projections = build_projections(store, allowed_source_ids=("fixture-file",))
    report = {"simulation_only": True, "partial": partial, "resumed": resumed,
              "rollback": rolled_back, "repeat_rollback": repeated,
              "mock_dispatch_count": calls, "restored_exact_name": adapter.states["fixture-file"]["name"],
              "restored_exact_parents": adapter.states["fixture-file"]["parents"],
              "journal_records": len(store.records()), "live_mutations": 0}
    return report, projections


def handoff_demo(root):
    """Bootstrap a separate synthetic handoff store, then run every API stage."""
    from .action_bridge import ActionBridge
    store = Journal(root)
    states = {"fixture-origin": fixture_state("fixture-origin", "Original", FOLDER, ()),
              "fixture-target": fixture_state("fixture-target", "Target", FOLDER, ()),
              "fixture-source": fixture_state("fixture-source", "Report.pdf", parents=("fixture-origin",))}
    adapter = MockAdapter(states)
    before = states["fixture-source"]
    op = Operation("fixture-handoff-op", "fixture-source", "MOVE", before,
                   {"parents": ["fixture-target"]}, proposal_refs=("MOVE-fixture",))
    plan = Plan("fixture-handoff-batch", "fixture-handoff-batch", "fixture-revision", (op,))
    Engine(store, adapter).register(plan)
    current = datetime.now(timezone.utc)
    approval = Approval(str(uuid4()), plan.run_id, plan.batch_id, plan.proposal_revision,
                        plan.proposal_hash, (op.operation_id,), "fixture-actor", current.isoformat(),
                        (current + timedelta(hours=1)).isoformat())
    check = adapter.preflight(op, before, set())
    check.update(ancestors_complete=True, ancestor_ids=[])
    proof = store.evidence({"mode": "TEST_ONLY", "mechanism": "FIXTURE_ATOMIC_CAS",
                            "enforced": True, "file_id": op.file_id,
                            "before_hash": digest(before), "safety_hash": digest(check)})
    check["concurrency"] = {"mechanism": "FIXTURE_ATOMIC_CAS", "enforced": True, "evidence_ref": proof}
    bridge = ActionBridge(store, mode="TEST_ONLY")
    envelope = bridge.prepare_action(plan, op.operation_id, approval, before, check)
    ticket = bridge.mark_attempt(envelope["envelope_hash"], before, check)
    try:
        bridge.mark_attempt(envelope["envelope_hash"], before, check)
    except SafetyError as error:
        duplicate_blocked = str(error)
    else:
        raise AssertionError("A consumed handoff ticket was reissued")
    # In-memory remote fixture only. No connector is called or resolved.
    raw = adapter.apply(op, before, adapter.preflight(op, before, set()), ticket["attempt_key"])
    bridge.ingest_result(envelope["envelope_hash"], ticket["attempt_key"], raw)
    observed = adapter.get(op.file_id)
    terminal = {"mode": "TEST_ONLY", "kind": "FIXTURE_TERMINAL", "terminal": True,
                "attempt_key": ticket["attempt_key"], "envelope_hash": envelope["envelope_hash"]}
    result = bridge.reconcile_action(envelope["envelope_hash"], observed, check, terminal)
    # Save exact JSON inputs to document each process boundary, including the
    # prerequisite frozen store and its content-addressed fixture proof.
    save_new(Path(root) / "fixture-prepare.json", {"plan": plan.body(), "operation_id": op.operation_id,
             "approval": approval.body(), "before": before, "preflight": check})
    save_new(Path(root) / "fixture-mark.json", {"envelope_hash": envelope["envelope_hash"],
             "observed_before": before, "preflight": check})
    save_new(Path(root) / "fixture-result.json", {"envelope_hash": envelope["envelope_hash"],
             "attempt_key": ticket["attempt_key"], "raw": raw})
    save_new(Path(root) / "fixture-reconcile.json", {"envelope_hash": envelope["envelope_hash"],
             "observed": observed, "postflight": check, "terminal_evidence": terminal})
    return {"simulation_only": True, "live_mutations": 0, "external_tool_calls": 0,
            "fixture_tool": ticket["tool"], "result": result["kind"],
            "duplicate_attempt_blocked": duplicate_blocked, "mock_dispatches": len(adapter.calls),
            "fixture_store": str(Path(root).absolute())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", default="private", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("export")
    p = sub.add_parser("evidence")
    p.add_argument("--input", required=True, type=Path)
    p = sub.add_parser("disable-agent-dispatch")
    p.add_argument("--controller-id", required=True)
    p.add_argument("--reason-code", default="HOLD")
    p = sub.add_parser("rollback-plan")
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--selected", nargs="+", required=True)
    p.add_argument("--batch-id", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--output", type=Path)
    p = sub.add_parser("handoff-demo")
    p.add_argument("--output", type=Path)
    p = sub.add_parser("backup")
    p.add_argument("destination", type=Path)
    p = sub.add_parser("prepare")
    p.add_argument("--plan", required=True, type=Path)
    p = sub.add_parser("receipt")
    p.add_argument("--request-id", required=True)
    p.add_argument("--file-id", required=True)
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--receipt-id")
    p = sub.add_parser("reconcile")
    p.add_argument("--request-id", required=True)
    p = sub.add_parser("import-workbook")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--metadata", required=True, type=Path)
    p.add_argument("--resolutions", required=True, type=Path)
    p.add_argument("--selected", nargs="+", required=True)
    p.add_argument("--batch-id", required=True)
    p.add_argument("--expected-input-sha256")
    p = sub.add_parser("import-section")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--projection", required=True, type=Path)
    p.add_argument("--owner-approval-ref", required=True)
    p.add_argument("--review-context-ref", required=True)
    p.add_argument("--metadata-profile", choices=("STRICT", "AGENT_ASSISTED"), default="STRICT")
    p.add_argument("--section-id", required=True)
    p.add_argument("--metadata", required=True, type=Path)
    p.add_argument("--resolutions", required=True, type=Path)
    p.add_argument("--batch-id", required=True)
    p = sub.add_parser("authorize-agent-dispatch")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--controller-id", required=True)
    for command in ("prepare-action", "mark-attempt", "ingest-result", "reconcile-action"):
        p = sub.add_parser(command)
        p.add_argument("--input", required=True, type=Path)
        modes = p.add_mutually_exclusive_group()
        modes.add_argument("--fixture-mode", action="store_true",
                       help="Synthetic handoff only, stored separately from real state")
        modes.add_argument("--agent-mode", action="store_true",
                           help="Local handoff only; requires reviewed private enablement before preparing or marking")
        p.add_argument("--controller-id")
    args = parser.parse_args()
    private = args.private_dir.absolute()
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private, 0o700)
    policy_file = private / "policy.json"
    if args.command == "init" and not policy_file.exists():
        save_new(policy_file, {"schema_version": 1, "live_mutations_enabled": False,
                              "agent_handoff_enabled": False,
                              "connector_mode": "agent_read_receipts_only", "denied_source_ids": [],
                              "workbook_id": None, "automatic_backups": False})
    if not policy_file.exists():
        raise SafetyError("RUN_INIT_AND_CONFIGURE_PRIVATE_POLICY")
    policy = read_json(policy_file)
    if policy.get("live_mutations_enabled") is not False:
        raise SafetyError("LIVE_EXECUTION_DISABLED")
    if policy.get("migration_paused") and args.command in {"prepare", "import-workbook", "import-section"}:
        raise SafetyError("MIGRATION_VALIDATION_PAUSE")
    if args.command == "import-workbook" and policy.get("required_method_profile"):
        raise SafetyError("REVIEWED_SECTION_PROFILE_REQUIRED")
    agent_selected = args.command == "authorize-agent-dispatch" or getattr(args, "agent_mode", False)
    if agent_selected and not args.controller_id:
        raise SafetyError("CONTROLLER_ID_REQUIRED")
    if agent_selected and args.command in {"authorize-agent-dispatch", "prepare-action", "mark-attempt"}:
        if policy.get("migration_paused"):
            raise SafetyError("MIGRATION_VALIDATION_PAUSE")
        if policy.get("agent_handoff_enabled") is not True:
            raise SafetyError("AGENT_HANDOFF_DISABLED")
    journal = Journal(private / "state")
    for file_id in policy.get("denied_source_ids", []):
        if not any(r["kind"] == "EXCLUDED" and r["payload"]["file_id"] == file_id for r in journal.records()):
            journal.append("EXCLUDED", payload={"file_id": file_id, "reason_code": "PERMANENT_WITHDRAWAL"})
    if args.command in ("init", "status"):
        records = journal.records()
        result = {"live_mutations_enabled": False, "connector_mode": "agent_read_receipts_only",
                  "migration_paused": policy.get("migration_paused", False),
                  "agent_handoff_enabled": policy.get("agent_handoff_enabled", False),
                  "required_method_profile": policy.get("required_method_profile"),
                  "private_store": str(journal.root), "journal_records": len(records),
                  "journal_tip": records[-1]["hash"] if records else None,
                  "denied_source_count": len(policy.get("denied_source_ids", []))}
    elif args.command == "demo":
        output = args.output or private / ("demo-" + str(uuid4()))
        result, projections = demo(output)
        result["report_path"] = save_new(output / "report.json", result)
        save_new(output / "projection-private.json", projections["workbook"])
        save_new(output / "projection-redacted.json", projections["html"])
    elif args.command == "handoff-demo":
        output = args.output or private / ("handoff-demo-" + str(uuid4()))
        result = handoff_demo(output)
        result["report_file"] = save_new(output / "report.json", result)
    elif args.command == "backup":
        result = journal.backup(args.destination)
    elif args.command == "evidence":
        result = {"evidence_ref": journal.evidence(read_json(args.input)), "private": True, "external_tool_calls": 0}
    elif args.command == "disable-agent-dispatch":
        from .action_bridge import ActionBridge
        record = ActionBridge(journal, mode="AGENT_ASSISTED", controller_id=args.controller_id).disable_agent_dispatch(args.reason_code)
        result = {"record_sequence": record["seq"], "agent_handoff_enabled": False, "external_tool_calls": 0}
    elif args.command == "rollback-plan":
        from .agent_profile import rollback_agent_plan
        result_plan = rollback_agent_plan(journal, plan_from_json(read_json(args.plan)), args.selected, args.batch_id)
        result = {"manifest_file": save_new(private / "imports" / (str(uuid4()) + "-rollback.json"), result_plan.body()),
                  "execution_approved": False, "external_tool_calls": 0}
    elif args.command == "export":
        projections = build_projections(journal)
        identifier = str(uuid4())
        result = {"private": save_new(private / "exports" / (identifier + "-workbook.json"), projections["workbook"]),
                  "redacted": save_new(private / "exports" / (identifier + "-html.json"), projections["html"]),
                  "published": False}
    elif args.command == "prepare":
        from .bridge import prepare_reads
        request = prepare_reads(journal, read_json(args.plan), set(policy.get("denied_source_ids", [])))
        result = {"request_id": request["request_id"], "read_only": True,
                  "request_file": save_new(private / "requests" / (request["request_id"] + ".json"), request)}
    elif args.command == "receipt":
        from .bridge import ingest_receipt
        ref = ingest_receipt(journal, args.request_id, args.file_id, read_json(args.input), receipt_id=args.receipt_id)
        result = {"receipt_ref": ref, "read_only": True, "action_success_claimed": False}
    elif args.command == "reconcile":
        from .bridge import reconcile_reads
        result = reconcile_reads(journal, args.request_id)
    elif args.command == "import-section":
        from .section_importer import import_section
        projection = read_json(args.projection)
        if policy.get("required_method_profile") and projection.get("method_profile") != policy["required_method_profile"]:
            raise SafetyError("METHOD_PROFILE_MISMATCH")
        imported = import_section(journal, read_json(args.input), projection=projection,
                                  owner_approval_ref=args.owner_approval_ref, expected_section_id=args.section_id,
                                  review_context_ref=args.review_context_ref,
                                  metadata_profile=args.metadata_profile,
                                  batch_id=args.batch_id, metadata_by_id=read_json(args.metadata),
                                  resolutions=read_json(args.resolutions), expected_workbook_id=policy.get("workbook_id"),
                                  expected_method_profile=policy.get("required_method_profile", projection.get("method_profile")))
        output = {"manifest": imported.manifest, "plan": imported.plan.body() if imported.plan else None,
                  "evidence_ref": imported.evidence_ref}
        result = {"manifest_file": save_new(private / "imports" / (str(uuid4()) + ".json"), output),
                  "executable_approval_created": False, "plan_ready_for_review": imported.plan is not None}
    elif args.command == "authorize-agent-dispatch":
        from .action_bridge import ActionBridge
        from .agent_profile import register_agent_plan
        payload = read_json(args.input)
        plan = plan_from_json(payload["plan"])
        require_policy_profile(journal, plan, policy)
        register_agent_plan(journal, plan)
        bridge = ActionBridge(journal, mode="AGENT_ASSISTED", controller_id=args.controller_id)
        result = bridge.authorize_agent_dispatch(plan, Approval(**payload["approval"]),
                                                 payload["authorization_evidence"], payload["enable_evidence"])
    elif args.command in ("prepare-action", "mark-attempt", "ingest-result", "reconcile-action"):
        from .action_bridge import ActionBridge
        mode = "TEST_ONLY" if args.fixture_mode else ("AGENT_ASSISTED" if args.agent_mode else "DISABLED")
        # Fixture approval/action records must never enter the real journal.
        action_journal = Journal(private / "handoff-fixture-state") if args.fixture_mode else journal
        bridge = ActionBridge(action_journal, mode=mode, controller_id=args.controller_id)
        payload = read_json(args.input)
        if args.command == "prepare-action":
            plan = plan_from_json(payload["plan"])
            if args.agent_mode:
                require_policy_profile(action_journal, plan, policy)
            if args.fixture_mode:
                Engine(action_journal, MockAdapter({})).register(plan)
            result = bridge.prepare_action(plan, payload["operation_id"],
                                           Approval(**payload["approval"]), payload["before"], payload["preflight"])
        elif args.command == "mark-attempt":
            if args.agent_mode:
                envelope, _ = bridge._load(payload["envelope_hash"])
                require_policy_profile(action_journal, plan_from_json(action_journal.read_evidence(envelope["plan_ref"])), policy)
            result = bridge.mark_attempt(payload["envelope_hash"], payload["observed_before"], payload["preflight"])
        elif args.command == "ingest-result":
            ref = bridge.ingest_result(payload["envelope_hash"], payload["attempt_key"], payload["raw"],
                                       receipt_id=payload.get("receipt_id"))
            result = {"receipt_ref": ref, "execution_ready": False, "action_success_claimed": False}
        else:
            result = bridge.reconcile_action(payload["envelope_hash"], payload["observed"],
                                              payload["postflight"], payload["terminal_evidence"])
    else:
        from .importer import import_workbook
        imported = import_workbook(journal, read_json(args.input), batch_id=args.batch_id,
                                   selected_refs=args.selected, metadata_by_id=read_json(args.metadata),
                                   resolutions=read_json(args.resolutions),
                                   expected_input_sha256=args.expected_input_sha256,
                                   expected_workbook_id=policy.get("workbook_id"))
        output = {"manifest": imported.manifest, "plan": imported.plan.body() if imported.plan else None,
                  "evidence_ref": imported.evidence_ref}
        result = {"manifest_file": save_new(private / "imports" / (str(uuid4()) + ".json"), output),
                  "approval_created": False, "plan_ready_for_review": imported.plan is not None}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (SafetyError, ValueError, KeyError, OSError) as error:
        raise SystemExit("Blocked: " + str(error))
