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
from .model import Approval, FOLDER, Operation, Plan, SafetyError
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", default="private", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("export")
    p = sub.add_parser("demo")
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
    args = parser.parse_args()
    private = args.private_dir.absolute()
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private, 0o700)
    policy_file = private / "policy.json"
    if args.command == "init" and not policy_file.exists():
        save_new(policy_file, {"schema_version": 1, "live_mutations_enabled": False,
                              "connector_mode": "agent_read_receipts_only", "denied_source_ids": [],
                              "workbook_id": None, "automatic_backups": False})
    if not policy_file.exists():
        raise SafetyError("RUN_INIT_AND_CONFIGURE_PRIVATE_POLICY")
    policy = read_json(policy_file)
    if policy.get("live_mutations_enabled") is not False:
        raise SafetyError("LIVE_EXECUTION_DISABLED")
    journal = Journal(private / "state")
    for file_id in policy.get("denied_source_ids", []):
        if not any(r["kind"] == "EXCLUDED" and r["payload"]["file_id"] == file_id for r in journal.records()):
            journal.append("EXCLUDED", payload={"file_id": file_id, "reason_code": "PERMANENT_WITHDRAWAL"})
    if args.command in ("init", "status"):
        records = journal.records()
        result = {"live_mutations_enabled": False, "connector_mode": "agent_read_receipts_only",
                  "private_store": str(journal.root), "journal_records": len(records),
                  "journal_tip": records[-1]["hash"] if records else None,
                  "denied_source_count": len(policy.get("denied_source_ids", []))}
    elif args.command == "demo":
        output = args.output or private / ("demo-" + str(uuid4()))
        result, projections = demo(output)
        result["report_path"] = save_new(output / "report.json", result)
        save_new(output / "projection-private.json", projections["workbook"])
        save_new(output / "projection-redacted.json", projections["html"])
    elif args.command == "backup":
        result = journal.backup(args.destination)
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
