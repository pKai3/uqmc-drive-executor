"""Real CLI process boundaries, with synthetic evidence and no external tools."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from uqmc_recovery.adapters import MockAdapter, fixture_state
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import Approval, FOLDER, Operation, Plan, digest


class ActionCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.private = self.root / "private"
        self.cli("init")
        self.store = Journal(self.private / "handoff-fixture-state")
        states = {"fixture-parent": fixture_state("fixture-parent", "Folder", FOLDER, ()),
                  "fixture-file": fixture_state("fixture-file", "Old.pdf", parents=("fixture-parent",))}
        self.before = states["fixture-file"]
        self.op = Operation("fixture-op", "fixture-file", "RENAME", self.before, {"name": "New.pdf"})
        self.plan = Plan("fixture-batch", "fixture-batch", "fixture-revision", (self.op,))
        current = datetime.now(timezone.utc)
        approval = Approval("fixture-approval", self.plan.run_id, self.plan.batch_id,
                            self.plan.proposal_revision, self.plan.proposal_hash, (self.op.operation_id,),
                            "fixture-actor", current.isoformat(), (current + timedelta(hours=1)).isoformat())
        self.check = MockAdapter(states).preflight(self.op, self.before, set())
        self.check.update(ancestors_complete=True, ancestor_ids=[])
        ref = self.store.evidence({"mode": "TEST_ONLY", "mechanism": "FIXTURE_ATOMIC_CAS", "enforced": True,
                                   "file_id": self.op.file_id, "before_hash": digest(self.before),
                                   "safety_hash": digest(self.check)})
        self.check["concurrency"] = {"mechanism": "FIXTURE_ATOMIC_CAS", "enforced": True, "evidence_ref": ref}
        self.prepare = {"plan": self.plan.body(), "operation_id": self.op.operation_id,
                        "approval": approval.body(), "before": self.before, "preflight": self.check}

    def cli(self, command, payload=None, fixture=False, success=True):
        args = [sys.executable, "-m", "uqmc_recovery", "--private-dir", str(self.private), command]
        if payload is not None:
            path = self.root / ("input-" + str(len(list(self.root.glob("input-*")))) + ".json")
            path.write_text(json.dumps(payload))
            args += ["--input", str(path)]
        if fixture:
            args += ["--fixture-mode"]
        result = subprocess.run(args, capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1])
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def test_default_disabled_does_not_freeze_real_plan(self):
        result = self.cli("prepare-action", self.prepare, success=False)
        self.assertIn("LIVE_EXECUTION_DISABLED", result.stderr)
        self.assertEqual(Journal(self.private / "state").records(), [])

    def test_agent_enable_and_pause_gates_run_before_plan_or_approval_writes(self):
        payload = self.root / "agent-input.json"
        payload.write_text("{}")
        args = [sys.executable, "-m", "uqmc_recovery", "--private-dir", str(self.private),
                "authorize-agent-dispatch", "--controller-id", "fixture-controller", "--input", str(payload)]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AGENT_HANDOFF_DISABLED", result.stderr)
        policy_path = self.private / "policy.json"
        policy = json.loads(policy_path.read_text())
        policy.update(agent_handoff_enabled=True, migration_paused=True)
        policy_path.write_text(json.dumps(policy))
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MIGRATION_VALIDATION_PAUSE", result.stderr)
        self.assertEqual(Journal(self.private / "state").records(), [])

    def test_raw_workbook_import_cannot_bypass_configured_section_profile(self):
        policy_path = self.private / "policy.json"
        policy = json.loads(policy_path.read_text())
        policy.update(required_method_profile="existing_folder_consolidation")
        policy_path.write_text(json.dumps(policy))
        args = [sys.executable, "-m", "uqmc_recovery", "--private-dir", str(self.private), "import-workbook",
                "--input", "missing", "--metadata", "missing", "--resolutions", "missing",
                "--selected", "MOVE-fixture", "--batch-id", "fixture-batch"]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REVIEWED_SECTION_PROFILE_REQUIRED", result.stderr)
        self.assertEqual(Journal(self.private / "state").records(), [])

    def test_separate_processes_cannot_reissue_consumed_ticket(self):
        envelope = self.cli("prepare-action", self.prepare, fixture=True)
        mark = {"envelope_hash": envelope["envelope_hash"], "observed_before": self.before, "preflight": self.check}
        ticket = self.cli("mark-attempt", mark, fixture=True)
        self.assertEqual(ticket["tool"], "fixture.drive.update_file")
        second = self.cli("mark-attempt", mark, fixture=True, success=False)
        self.assertIn("HANDOFF_ATTEMPT_CONSUMED_RECONCILE", second.stderr)
        self.cli("ingest-result", {"envelope_hash": envelope["envelope_hash"], "attempt_key": ticket["attempt_key"],
                                   "raw": {"simulation": True, "success": True}}, fixture=True)
        self.assertEqual(self.store.records()[-1]["kind"], "UNCERTAIN")
        observed = self.op.intended()
        observed.update(version="2", modified_time="2026-01-01T00:00:02Z")
        outcome = self.cli("reconcile-action", {"envelope_hash": envelope["envelope_hash"], "observed": observed,
                           "postflight": self.check, "terminal_evidence": {"mode": "TEST_ONLY", "kind": "FIXTURE_TERMINAL",
                           "terminal": True, "envelope_hash": envelope["envelope_hash"], "attempt_key": ticket["attempt_key"]}}, fixture=True)
        self.assertEqual(outcome["kind"], "RECONCILED_AT_TARGET")
        self.assertEqual(Journal(self.private / "state").records(), [])

    def test_changed_preimage_fails_before_marking_attempt(self):
        envelope = self.cli("prepare-action", self.prepare, fixture=True)
        altered = copy.deepcopy(self.before)
        altered["name"] = "Manual.pdf"
        result = self.cli("mark-attempt", {"envelope_hash": envelope["envelope_hash"],
                          "observed_before": altered, "preflight": self.check}, fixture=True, success=False)
        self.assertIn("HANDOFF_BASELINE_DRIFT", result.stderr)
        self.assertFalse(any(r["kind"] == "ATTEMPTED" for r in self.store.records()))


if __name__ == "__main__":
    unittest.main()
