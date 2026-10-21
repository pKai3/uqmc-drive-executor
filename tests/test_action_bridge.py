import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import uuid

from uqmc_recovery.action_bridge import ActionBridge
from uqmc_recovery.adapters import MockAdapter, fixture_state
from uqmc_recovery.engine import Engine
from uqmc_recovery.journal import Journal, JournalCorrupt
from uqmc_recovery.model import Approval, FOLDER, Operation, Plan, SafetyError, digest


class ProcessDeath(BaseException):
    pass


class ActionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.make_context(self.root / "state")

    def tearDown(self):
        self.temp.cleanup()

    def make_context(self, path):
        self.journal = Journal(path)
        self.before = fixture_state("fixture-source", "Budget.pdf", parents=["fixture-origin"])
        self.adapter = MockAdapter({"fixture-source": copy.deepcopy(self.before),
            "fixture-origin": fixture_state("fixture-origin", "Original", FOLDER, ()),
            "fixture-destination": fixture_state("fixture-destination", "Destination", FOLDER, ())})
        self.engine = Engine(self.journal, self.adapter)
        self.plan = Plan("batch", "batch", "r1", (
            Operation("batch:rename", "fixture-source", "RENAME", copy.deepcopy(self.before), {"name": "New.pdf"}),))
        self.engine.register(self.plan)
        clock = datetime.now(timezone.utc)
        self.approval = Approval("fixture-approval", "batch", "batch", "r1", self.plan.proposal_hash,
            ("batch:rename",), "fixture-user", (clock - timedelta(minutes=1)).isoformat(),
            (clock + timedelta(hours=1)).isoformat(), "TEST_ONLY")
        self.check = self.make_check(self.plan.operations[0])
        self.bridge = ActionBridge(self.journal, "TEST_ONLY")

    def make_check(self, op, before=None):
        before = before or op.before
        check = self.adapter.preflight(op, before, set())
        check.update(ancestors_complete=True, ancestor_ids=[])
        proof = {"mode": "TEST_ONLY", "mechanism": "FIXTURE_ATOMIC_CAS", "enforced": True,
                 "file_id": op.file_id, "before_hash": digest(before), "safety_hash": digest(check)}
        check["concurrency"] = {"mechanism": "FIXTURE_ATOMIC_CAS", "enforced": True,
                                "evidence_ref": self.journal.evidence(proof)}
        return check

    def prepare(self):
        return self.bridge.prepare_action(self.plan, self.plan.operations[0].operation_id,
                                          self.approval, self.before, self.check)

    def mark(self, envelope):
        return self.bridge.mark_attempt(envelope["envelope_hash"], self.before, self.check)

    def terminal(self, envelope, value=True):
        return {"kind": "FIXTURE_TERMINAL", "mode": "TEST_ONLY", "terminal": value,
                "attempt_key": envelope["attempt_key"], "envelope_hash": envelope["envelope_hash"]}

    def target(self, envelope):
        after = copy.deepcopy(self.check["expected_after"])
        after["version"] = "2"
        after["modified_time"] = "2026-01-01T00:00:02Z"
        return after

    def test_default_and_every_live_mode_reject_without_ticket(self):
        with self.assertRaisesRegex(SafetyError, "DISABLED"):
            ActionBridge(self.journal).prepare_action(self.plan, "batch:rename", self.approval, self.before, self.check)
        for mode in ("LIVE", "AGENT", "APPROVED", "LIVE_APPROVED"):
            with self.assertRaisesRegex(SafetyError, "DISABLED"):
                ActionBridge(self.journal, mode)
        self.assertFalse(any(r["kind"] == "ATTEMPTED" for r in self.journal.records()))

    def test_exact_fake_tool_envelope_and_attempt_before_ticket(self):
        envelope = self.prepare()
        self.assertFalse(envelope["dispatch_authorized"])
        self.assertEqual(envelope["args"], {"fileId": "fixture-source", "name": "New.pdf"})
        self.assertEqual(envelope["tool"], "fixture.drive.update_file")
        self.assertEqual(digest({k: v for k, v in envelope.items() if k != "envelope_hash"}), envelope["envelope_hash"])
        ticket = self.mark(envelope)
        self.assertEqual(ticket["args"], envelope["args"])
        self.assertFalse(ticket["live_execution_enabled"])
        self.assertEqual(self.journal.records()[-1]["kind"], "ATTEMPTED")
        self.assertEqual(self.adapter.calls, [])
        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
            self.mark(envelope)

    def test_move_envelope_only_changes_exact_parent_delta(self):
        plan = Plan("move", "move", "r1", (Operation("move:1", "fixture-source", "MOVE", self.before,
                                                     {"parents": ["fixture-destination"]}),))
        self.engine.register(plan)
        auth = replace(self.approval, approval_id="move-approval", run_id="move", batch_id="move",
                       proposal_hash=plan.proposal_hash, operation_ids=("move:1",))
        check = self.make_check(plan.operations[0])
        envelope = self.bridge.prepare_action(plan, "move:1", auth, self.before, check)
        self.assertEqual(envelope["args"], {"fileId": "fixture-source", "addParents": "fixture-destination",
                                            "removeParents": "fixture-origin"})

    def test_move_then_rename_uses_verified_dependency_version(self):
        move = Operation("compound:move", "fixture-source", "MOVE", self.before, {"parents": ["fixture-destination"]})
        rename = Operation("compound:rename", "fixture-source", "RENAME", move.intended(), {"name": "New.pdf"},
                           (move.operation_id,))
        plan = Plan("compound", "compound", "r1", (move, rename))
        self.engine.register(plan)
        auth = replace(self.approval, approval_id="compound-approval", run_id="compound", batch_id="compound",
                       proposal_hash=plan.proposal_hash, operation_ids=(move.operation_id, rename.operation_id))
        check = self.make_check(move)
        first = self.bridge.prepare_action(plan, move.operation_id, auth, self.before, check)
        self.bridge.mark_attempt(first["envelope_hash"], self.before, check)
        moved = copy.deepcopy(check["expected_after"])
        moved.update(version="2", modified_time="2026-01-01T00:00:02Z")
        self.bridge.reconcile_action(first["envelope_hash"], moved, check, self.terminal(first))
        self.adapter.states["fixture-source"] = moved
        rename_check = self.make_check(rename, moved)
        second = self.bridge.prepare_action(plan, rename.operation_id, auth, moved, rename_check)
        self.bridge.mark_attempt(second["envelope_hash"], moved, rename_check)
        renamed = copy.deepcopy(rename_check["expected_after"])
        renamed.update(version="3", modified_time="2026-01-01T00:00:03Z")
        result = self.bridge.reconcile_action(second["envelope_hash"], renamed, rename_check, self.terminal(second))
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")
        self.assertEqual([r["payload"]["action"] for r in self.journal.records() if r["kind"] == "ATTEMPTED"],
                         ["MOVE", "RENAME"])

    def test_superseded_prepared_state_cannot_dispatch_old_envelope(self):
        envelope = self.prepare()
        self.journal.append("BLOCKED_CONFLICT", operation_id="batch:rename", payload={"file_id": "fixture-source"})
        with self.assertRaisesRegex(SafetyError, "STATE_CHANGED"):
            self.mark(envelope)

    def test_raw_success_remains_uncertain_and_private(self):
        envelope = self.prepare()
        self.mark(envelope)
        raw = {"id": "fixture-source", "success": True, "private_error": "DO_NOT_PUBLISH_RAW"}
        ref = self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], raw)
        self.assertEqual(self.journal.read_evidence(ref)["raw"], raw)
        self.assertEqual(self.journal.records()[-1]["kind"], "UNCERTAIN")
        self.assertNotIn("DO_NOT_PUBLISH_RAW", self.journal.path.read_text())
        self.assertFalse(any(r["kind"] == "VERIFIED_APPLIED" for r in self.journal.records()))

    def test_reconciliation_target_is_attribution_uncertain(self):
        envelope = self.prepare()
        self.mark(envelope)
        result = self.bridge.reconcile_action(envelope["envelope_hash"], self.target(envelope), self.check,
                                              self.terminal(envelope))
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")
        self.assertEqual(result["payload"]["reason_code"], "OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN")
        saved = len(self.journal.records())
        self.assertEqual(self.bridge.reconcile_action(envelope["envelope_hash"], self.target(envelope), self.check,
                                                       self.terminal(envelope)), result)
        self.assertEqual(len(self.journal.records()), saved)
        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
            self.mark(envelope)

    def test_preimage_without_terminal_stays_fenced(self):
        envelope = self.prepare()
        self.mark(envelope)
        result = self.bridge.reconcile_action(envelope["envelope_hash"], self.before, self.check,
                                              self.terminal(envelope, False))
        self.assertEqual(result["kind"], "UNCERTAIN")
        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
            self.mark(envelope)
        result = self.bridge.reconcile_action(envelope["envelope_hash"], self.before, self.check,
                                              self.terminal(envelope))
        self.assertEqual(result["kind"], "FAILED_NOT_APPLIED")
        with self.assertRaisesRegex(SafetyError, "ATTEMPTED_OR_FINISHED"):
            self.prepare()

    def test_manual_changes_or_postflight_changes_are_conflicts(self):
        for change in ("name", "parents", "permissions", "postflight"):
            with self.subTest(change=change):
                self.make_context(self.root / change)
                envelope = self.prepare()
                self.mark(envelope)
                observed, postflight = self.target(envelope), copy.deepcopy(self.check)
                if change == "name":
                    observed["name"] = "Manual.pdf"
                elif change == "parents":
                    observed["parents"] = ["fixture-destination"]
                elif change == "permissions":
                    observed["permissions"].append({"id": "other", "role": "reader"})
                else:
                    postflight["collision_ids"] = ["collision"]
                result = self.bridge.reconcile_action(envelope["envelope_hash"], observed, postflight, self.terminal(envelope))
                self.assertEqual(result["kind"], "BLOCKED_CONFLICT")

    def test_stale_revoked_excluded_and_manual_drift_block_mark(self):
        for change in ("revoked", "excluded", "proposal_excluded", "manual", "expired"):
            with self.subTest(change=change):
                self.make_context(self.root / change)
                if change == "expired":
                    self.approval = replace(self.approval, expires_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
                envelope = self.prepare()
                before = copy.deepcopy(self.before)
                if change == "revoked":
                    self.engine.revoke(self.approval.approval_id)
                elif change == "excluded":
                    self.engine.exclude("fixture-origin")
                elif change == "proposal_excluded":
                    self.journal.append("EXCLUDED", payload={"file_id": "fixture-source"})
                elif change == "manual":
                    before["name"] = "Manual.pdf"
                else:
                    import uqmc_recovery.action_bridge as module
                    from unittest import mock
                    future = datetime.now(timezone.utc) + timedelta(minutes=10)
                    class FutureClock:
                        @staticmethod
                        def now(tz):
                            return future
                    with mock.patch.object(module, "datetime", FutureClock), self.assertRaisesRegex(SafetyError, "EXPIRED"):
                        self.bridge.mark_attempt(envelope["envelope_hash"], before, self.check)
                    continue
                with self.assertRaises(SafetyError):
                    self.bridge.mark_attempt(envelope["envelope_hash"], before, self.check)
                self.assertFalse(any(r["kind"] == "ATTEMPTED" for r in self.journal.records()))

    def test_late_receipts_and_reconciliation_survive_revocation_or_exclusion(self):
        envelope = self.prepare()
        self.mark(envelope)
        self.engine.revoke(self.approval.approval_id)
        self.engine.exclude("fixture-source")
        reopened = ActionBridge(Journal(self.journal.root))
        reopened.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], {"id": "fixture-source"})
        result = reopened.reconcile_action(envelope["envelope_hash"], self.target(envelope), self.check, self.terminal(envelope))
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")

    def test_duplicate_mismatched_results_and_attempt_keys_are_rejected(self):
        envelope = self.prepare()
        with self.assertRaisesRegex(SafetyError, "NOT_ATTEMPTED"):
            self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], {})
        self.mark(envelope)
        with self.assertRaisesRegex(SafetyError, "KEY_MISMATCH"):
            self.bridge.ingest_result(envelope["envelope_hash"], "wrong", {})
        with self.assertRaisesRegex(SafetyError, "SOURCE_MISMATCH"):
            self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], {"id": "wrong"})
        receipt_id = str(uuid.uuid4())
        raw = {"id": "fixture-source", "status": "success"}
        ref = self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], raw, receipt_id)
        saved = len(self.journal.records())
        self.assertEqual(self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], raw, receipt_id), ref)
        self.assertEqual(len(self.journal.records()), saved)
        with self.assertRaisesRegex(SafetyError, "CONFLICT"):
            self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], {"id": "fixture-source", "status": "other"})

    def test_reservations_survive_restart_and_block_parent_or_ancestor_overlap(self):
        envelope = self.prepare()
        for file_id, parent in (("fixture-other", "fixture-origin"), ("fixture-origin", "fixture-unrelated")):
            with self.subTest(file_id=file_id):
                before = fixture_state(file_id, "Other.pdf", parents=[parent])
                self.adapter.states[file_id] = before
                self.adapter.states.setdefault(parent, fixture_state(parent, "Parent", FOLDER, ()))
                batch = file_id + "-batch"
                plan = Plan(batch, batch, "r1", (Operation(batch + ":rename", file_id, "RENAME", before, {"name": "After.pdf"}),))
                auth = replace(self.approval, approval_id=batch + ":approval", run_id=batch, batch_id=batch,
                               proposal_hash=plan.proposal_hash, operation_ids=(batch + ":rename",))
                check = self.make_check(plan.operations[0])
                reopened = ActionBridge(Journal(self.journal.root), "TEST_ONLY")
                with self.assertRaisesRegex(SafetyError, "SCOPE_RESERVED|UNRESOLVED_PRIOR_REQUEST"):
                    self.engine.register(plan)
                    reopened.prepare_action(plan, plan.operations[0].operation_id, auth, before, check)
        self.assertEqual(self.prepare()["envelope_hash"], envelope["envelope_hash"])

    def test_synchronous_engine_cannot_bypass_prepared_or_attempted_handoff(self):
        envelope = self.prepare()
        with self.assertRaises(SafetyError):
            self.engine.run(self.plan, self.approval)
        self.assertEqual(self.adapter.calls, [])
        self.mark(envelope)
        with self.assertRaises(SafetyError):
            self.engine.run(self.plan, self.approval)
        with self.assertRaises(SafetyError):
            self.engine.reconcile(self.plan, self.plan.operations[0])
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.journal.records()[-1]["kind"], "ATTEMPTED")

    def test_hash_tamper_and_missing_fixture_cas_fail_closed(self):
        check = copy.deepcopy(self.check)
        check["concurrency"]["enforced"] = False
        with self.assertRaisesRegex(SafetyError, "CONCURRENCY"):
            self.bridge.prepare_action(self.plan, "batch:rename", self.approval, self.before, check)
        envelope = self.prepare()
        event = [r for r in self.journal.records() if r["kind"] == "PREPARED"][0]
        ref = event["payload"]["envelope_ref"]
        (self.journal.evidence_dir / (ref[7:] + ".json")).write_text("{}")
        with self.assertRaises(JournalCorrupt):
            self.mark(envelope)

    def test_exact_approval_binding_and_terminal_binding_are_required(self):
        for changes in ({"proposal_hash": "0" * 64}, {"proposal_revision": "stale"},
                        {"operation_ids": ()}, {"actor": ""}, {"mode": "LIVE"}):
            with self.subTest(changes=changes), self.assertRaises(SafetyError):
                self.bridge.prepare_action(self.plan, "batch:rename", replace(self.approval, **changes),
                                           self.before, self.check)
        envelope = self.prepare()
        self.mark(envelope)
        for changes in ({"attempt_key": "wrong"}, {"envelope_hash": "0" * 64}, {"mode": "LIVE"},
                        {"kind": "PROVIDER_ASSUMED_TERMINAL"}, {"terminal": "true"}):
            proof = self.terminal(envelope) | changes
            with self.subTest(changes=changes), self.assertRaisesRegex(SafetyError, "TERMINAL_PROOF"):
                self.bridge.reconcile_action(envelope["envelope_hash"], self.target(envelope), self.check, proof)
        self.assertEqual(self.journal.records()[-1]["kind"], "ATTEMPTED")

    def test_crash_after_reconciliation_evidence_never_reissues_ticket(self):
        envelope = self.prepare()
        self.mark(envelope)
        def fail(point):
            if point == "after_reconciliation_evidence":
                raise ProcessDeath()
        self.bridge.fault = fail
        with self.assertRaises(ProcessDeath):
            self.bridge.reconcile_action(envelope["envelope_hash"], self.target(envelope), self.check, self.terminal(envelope))
        self.bridge = ActionBridge(Journal(self.journal.root), "TEST_ONLY")
        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
            self.mark(envelope)
        result = self.bridge.reconcile_action(envelope["envelope_hash"], self.target(envelope), self.check, self.terminal(envelope))
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")

    def test_crash_before_after_prepare_and_approval_transitions_is_resumable(self):
        points = ["before_APPROVED", "after_APPROVED", "after_handoff_evidence", "before_PREPARED", "after_PREPARED"]
        for point in points:
            with self.subTest(point=point):
                self.make_context(self.root / point)
                def fail(actual):
                    if actual == point:
                        raise ProcessDeath()
                self.bridge.fault = fail
                with self.assertRaises(ProcessDeath):
                    self.prepare()
                self.bridge = ActionBridge(Journal(self.journal.root), "TEST_ONLY")
                envelope = self.prepare()
                self.mark(envelope)
                self.assertEqual(len([r for r in self.journal.records() if r["kind"] == "ATTEMPTED"]), 1)

    def test_crash_attempt_windows_never_reissue_uncertain_ticket(self):
        for point in ("after_final_handoff_check", "before_ATTEMPTED", "after_ATTEMPTED", "before_ticket_return"):
            with self.subTest(point=point):
                self.make_context(self.root / point)
                envelope = self.prepare()
                def fail(actual):
                    if actual == point:
                        raise ProcessDeath()
                self.bridge.fault = fail
                with self.assertRaises(ProcessDeath):
                    self.mark(envelope)
                self.bridge = ActionBridge(Journal(self.journal.root), "TEST_ONLY")
                if any(r["kind"] == "ATTEMPTED" for r in self.journal.records()):
                    with self.assertRaisesRegex(SafetyError, "CONSUMED"):
                        self.mark(envelope)
                    result = self.bridge.reconcile_action(envelope["envelope_hash"], self.before, self.check,
                                                          self.terminal(envelope, False))
                    self.assertEqual(result["kind"], "UNCERTAIN")
                else:
                    self.mark(envelope)
                self.assertEqual(len([r for r in self.journal.records() if r["kind"] == "ATTEMPTED"]), 1)

    def test_crash_result_windows_never_turn_raw_response_into_success(self):
        points = ["after_result_evidence", "before_HANDOFF_RESULT_RECEIVED", "after_HANDOFF_RESULT_RECEIVED",
                  "before_UNCERTAIN", "after_UNCERTAIN"]
        for point in points:
            with self.subTest(point=point):
                self.make_context(self.root / point)
                envelope = self.prepare()
                self.mark(envelope)
                def fail(actual):
                    if actual == point:
                        raise ProcessDeath()
                self.bridge.fault = fail
                with self.assertRaises(ProcessDeath):
                    self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], {"success": True})
                self.bridge = ActionBridge(Journal(self.journal.root), "TEST_ONLY")
                self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], {"success": True})
                self.assertEqual(self.journal.records()[-1]["kind"], "UNCERTAIN")
                self.assertEqual(len([r for r in self.journal.records() if r["kind"] == "HANDOFF_RESULT_RECEIVED"]), 1)

    def test_crash_reconciliation_transitions_preserve_outcome(self):
        for kind in ("RECONCILED_AT_TARGET", "FAILED_NOT_APPLIED", "BLOCKED_CONFLICT", "UNCERTAIN"):
            for side in ("before_", "after_"):
                point = side + kind
                with self.subTest(point=point):
                    self.make_context(self.root / point)
                    envelope = self.prepare()
                    self.mark(envelope)
                    observed = self.target(envelope) if kind == "RECONCILED_AT_TARGET" else copy.deepcopy(self.before)
                    if kind == "BLOCKED_CONFLICT":
                        observed["name"] = "Manual.pdf"
                    terminal = self.terminal(envelope, kind != "UNCERTAIN")
                    def fail(actual):
                        if actual == point:
                            raise ProcessDeath()
                    self.bridge.fault = fail
                    with self.assertRaises(ProcessDeath):
                        self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check, terminal)
                    self.bridge = ActionBridge(Journal(self.journal.root), "TEST_ONLY")
                    result = self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check, terminal)
                    self.assertEqual(result["kind"], kind)
                    with self.assertRaisesRegex(SafetyError, "CONSUMED"):
                        self.mark(envelope)


class AgentAssistedBridgeTests(unittest.TestCase):
    """Synthetic evidence only; no connector calls or real file identifiers."""

    def setUp(self):
        from uqmc_recovery.agent_profile import agent_snapshot, register_agent_plan
        from uqmc_recovery.action_bridge import AGENT_LIMITATIONS
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / "agent-fixture-state")
        self.snapshot = agent_snapshot
        self.before = agent_snapshot({"id": "agent-fixture-source", "name": "Original.pdf", "mime_type": "application/pdf",
            "parents": ["agent-fixture-origin"], "parents_complete": True,
            "permissions": [{"id": "direct", "role": "writer", "permissionDetails": [{"inherited": False}]}], "version": None})
        def folder(key, members):
            return agent_snapshot({"id": key, "name": key, "mime_type": FOLDER, "parents": [], "parents_complete": True,
                "subtree": {"complete": True, "membership_scope": "DIRECT_CHILDREN", "members": [{"id": x} for x in members]},
                "permissions": [{"id": "known-user", "role": "writer"}]})
        self.parents = {"agent-fixture-origin": folder("agent-fixture-origin", [self.before["id"]]),
                        "agent-fixture-target": folder("agent-fixture-target", [])}
        self.plan = Plan("agent-fixture-batch", "agent-fixture-batch", "r1", (
            Operation("agent-fixture:move", self.before["id"], "MOVE", self.before, {"parents": ["agent-fixture-target"]}),))
        register_agent_plan(self.journal, self.plan)
        clock = datetime.now(timezone.utc)
        self.approval = Approval("agent-fixture-approval", self.plan.run_id, self.plan.batch_id, "r1", self.plan.proposal_hash,
            (self.plan.operations[0].operation_id,), "synthetic-user", (clock-timedelta(minutes=1)).isoformat(),
            (clock+timedelta(hours=1)).isoformat(), "AGENT_ASSISTED")
        self.bridge = ActionBridge(self.journal, "AGENT_ASSISTED", controller_id="synthetic-controller")
        self.auth = {"kind": "EXPLICIT_BATCH_AUTHORIZATION", "authority": "human_user", "authorized": True,
            "actor": self.approval.actor, "approval_hash": digest(self.approval.body()), "proposal_hash": self.plan.proposal_hash,
            "provenance_ref": self.journal.evidence({"kind": "TRUSTED_USER_INSTRUCTION", "text": "Synthetic test only."}),
            "limitations_acknowledged": sorted(AGENT_LIMITATIONS), "access_policy": "SCOPED_EXPOSURE_ACCEPTED",
            "exposure_review_ref": self.journal.evidence({"kind": "SCOPED_EXPOSURE_REVIEW", "proposal_hash": self.plan.proposal_hash,
                "operation_ids": list(self.approval.operation_ids), "warning_presented": True, "approved": True,
                "newly_exposed_principals": ["synthetic-principal"], "unknown_exposure": True,
                "access_baselines": {value["id"]: self.journal.evidence(value) for value in [self.before, *self.parents.values()]}})}
        self.enable = {"kind": "DISPATCH_ENABLE_REVIEW", "controller_id": "synthetic-controller", "proposal_hash": self.plan.proposal_hash,
            "approval_id": self.approval.approval_id, "authorization_hash": digest(self.auth), "manifest_verified": True,
            "reviewer": "synthetic-reviewer", "reviewed_at": clock.isoformat(), "expires_at": (clock+timedelta(minutes=30)).isoformat(),
            "provenance_ref": self.journal.evidence({"kind": "TRUSTED_CONTROLLER_REVIEW", "test_only": True})}

    def tearDown(self):
        self.temp.cleanup()

    def read_proof(self, value):
        raw = {key: copy.deepcopy(value[key]) for key in ("id", "name", "mime_type", "parents", "version", "modified_time", "trashed", "permissions", "owners", "drive_id", "capabilities", "access") if key in value}
        return {"kind": "AGENT_CONNECTOR_READ", "observed_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_ref": self.journal.evidence(value), "receipt_ref": self.journal.evidence({"structuredContent": raw})}

    def check(self, observed=None, parents=None, before=None, op=None):
        before, observed = before or self.before, observed or before or self.before
        parents = copy.deepcopy(parents or self.parents)
        op = op or self.plan.operations[0]
        required = set(before["parents"]) | set(op.target.get("parents", []))
        parents = {key: value for key, value in parents.items() if key in required}
        expected = op.intended(before)
        check = {"complete": True, "no_cycle": True, "parents": parents,
            "parent_reads": {key: self.read_proof(value) for key, value in parents.items()}, "source_read": self.read_proof(observed),
            "collision_ids": [], "collision_review_ref": self.journal.evidence({"kind": "COLLISION_REVIEW",
                "proposal_hash": self.plan.proposal_hash, "operation_id": op.operation_id, "complete": True,
                "target_name": expected["name"], "target_parent_ids": expected["parents"], "collision_ids": [],
                "observed_at": datetime.now(timezone.utc).isoformat()}), "expected_after": expected, "ancestor_ids": [],
            "concurrency": {"mechanism": "SINGLE_CONTROLLER_FRESH_READS", "controller_id": "synthetic-controller",
                            "atomic_precondition": False, "race_window_acknowledged": True}}
        if before["mime_type"] == FOLDER:
            check["membership_ref"] = self.journal.evidence({"kind": "COMPLETE_MEMBERSHIP_READ", "root_id": before["id"],
                "complete": True, "pagination_complete": True, "membership_scope": "DESCENDANTS",
                "member_ids": [m["id"] for m in observed["subtree"]["members"]],
                "observed_at": datetime.now(timezone.utc).isoformat()})
        return check

    def configure(self, operations, parents=None):
        from uqmc_recovery.agent_profile import register_agent_plan
        self.parents = parents or self.parents
        self.plan = Plan("agent-fixture-rebound", "agent-fixture-rebound", "r2", tuple(operations))
        register_agent_plan(self.journal, self.plan)
        self.approval = replace(self.approval, approval_id="agent-fixture-rebound-approval", run_id=self.plan.run_id,
            batch_id=self.plan.batch_id, proposal_revision="r2", proposal_hash=self.plan.proposal_hash,
            operation_ids=tuple(op.operation_id for op in operations))
        exposure = self.journal.read_evidence(self.auth["exposure_review_ref"])
        required = {op.file_id for op in operations}
        for op in operations:
            required |= set(op.before["parents"]) | set(op.target.get("parents", []))
        available = {value["id"]: value for value in [*(op.before for op in operations), *self.parents.values()]}
        exposure.update(proposal_hash=self.plan.proposal_hash, operation_ids=list(self.approval.operation_ids),
            access_baselines={file_id: self.journal.evidence(available[file_id]) for file_id in required})
        self.auth.update(approval_hash=digest(self.approval.body()), proposal_hash=self.plan.proposal_hash,
                         exposure_review_ref=self.journal.evidence(exposure))
        self.enable.update(proposal_hash=self.plan.proposal_hash, approval_id=self.approval.approval_id,
                           authorization_hash=digest(self.auth))

    def authorize(self):
        return self.bridge.authorize_agent_dispatch(self.plan, self.approval, self.auth, self.enable)

    def prepare(self):
        self.authorize()
        return self.bridge.prepare_action(self.plan, self.plan.operations[0].operation_id, self.approval, self.before, self.check())

    def mark(self, envelope, before=None):
        before = before or self.before
        return self.bridge.mark_attempt(envelope["envelope_hash"], before, self.check(before=before))

    def completed(self, envelope, error=False):
        from uqmc_recovery.action_bridge import AGENT_TOOL
        raw = {"structuredContent": {"id": envelope["file_id"]}}
        if error:
            raw["isError"] = True
        ref = self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], raw)
        proof = self.journal.evidence({"kind": "TRUSTED_TOOL_COMPLETION", "tool": AGENT_TOOL,
            "tool_call_id": "synthetic-call", "outcome": "completed_success", "args_hash": digest(envelope["args"]),
            "raw_response_hash": digest(raw), "completed_at": datetime.now(timezone.utc).isoformat()})
        return {"kind": "AGENT_TOOL_COMPLETED", "mode": "AGENT_ASSISTED", "terminal": True,
            "attempt_key": envelope["attempt_key"], "envelope_hash": envelope["envelope_hash"], "response_ref": ref, "provenance_ref": proof}

    def refused(self, envelope, definitive=True):
        from uqmc_recovery.action_bridge import AGENT_TOOL
        raw = {"isError": True, "error": {"code": "SYNTHETIC_REFUSAL" if definitive else "SYNTHETIC_TIMEOUT"}}
        ref = self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], raw)
        proof = {"kind": "TRUSTED_TOOL_NONDISPATCH", "tool": AGENT_TOOL,
            "tool_call_id": "synthetic-refusal", "outcome": "not_dispatched", "args_hash": digest(envelope["args"]),
            "raw_response_hash": digest(raw), "completed_at": datetime.now(timezone.utc).isoformat(),
            "rejection_stage": "before_external_tool", "dispatcher_attested": definitive, "external_request_sent": False}
        return {"kind": "AGENT_TOOL_NOT_DISPATCHED", "mode": "AGENT_ASSISTED", "terminal": True,
            "attempt_key": envelope["attempt_key"], "envelope_hash": envelope["envelope_hash"],
            "response_ref": ref, "provenance_ref": self.journal.evidence(proof)}

    def after(self):
        observed, parents = self.snapshot(self.plan.operations[0].intended(self.before)), copy.deepcopy(self.parents)
        parents["agent-fixture-origin"]["subtree"]["members"] = []
        parents["agent-fixture-target"]["subtree"]["members"] = [{"id": self.before["id"]}]
        return observed, parents

    def test_unknowns_and_no_cas_with_exact_metadata_only_ticket(self):
        from uqmc_recovery.action_bridge import AGENT_TOOL
        envelope = self.prepare()
        self.assertIsNone(self.journal.read_evidence(envelope["before_ref"])["version"])
        self.assertNotIn("capabilities", self.journal.read_evidence(envelope["before_ref"]))
        self.assertFalse(envelope["atomic_precondition"])
        ticket = self.mark(envelope)
        self.assertEqual(ticket["tool"], AGENT_TOOL)
        self.assertEqual(set(ticket["args"]), {"fileId", "addParents", "removeParents"})
        self.assertTrue(ticket["agent_dispatch_authorized"])
        self.assertEqual(self.journal.records()[-1]["kind"], "ATTEMPTED")

    def test_explicit_separate_authorization_and_controller_enable_required(self):
        with self.assertRaisesRegex(SafetyError, "NOT_ENABLED"):
            self.bridge.prepare_action(self.plan, self.plan.operations[0].operation_id, self.approval, self.before, self.check())
        for key, value in (("proposal_hash", "0"*64), ("authorized", False), ("limitations_acknowledged", [])):
            with self.subTest(key=key), self.assertRaises(SafetyError):
                self.bridge.authorize_agent_dispatch(self.plan, self.approval, self.auth | {key: value}, self.enable)
        self.authorize()
        other = ActionBridge(self.journal, "AGENT_ASSISTED", controller_id="other-controller")
        with self.assertRaisesRegex(SafetyError, "SCOPE_MISMATCH"):
            other.prepare_action(self.plan, self.plan.operations[0].operation_id, self.approval, self.before, self.check())

    def test_fresh_capture_timestamp_allowed_but_old_read_blocked(self):
        self.authorize()
        old = self.check()
        envelope = self.bridge.prepare_action(self.plan, self.plan.operations[0].operation_id, self.approval, self.before, old)
        with self.assertRaisesRegex(SafetyError, "STALE"):
            self.bridge.mark_attempt(envelope["envelope_hash"], self.before, old)
        fresh = self.before | {"observed_at": datetime.now(timezone.utc).isoformat()}
        self.mark(envelope, fresh)

    def test_unproven_optional_values_and_known_version_drift_block(self):
        self.authorize()
        changed = self.before | {"version": "invented"}
        with self.assertRaisesRegex(SafetyError, "BASELINE_DRIFT"):
            self.bridge.prepare_action(self.plan, self.plan.operations[0].operation_id, self.approval, changed, self.check(before=changed))
        changed = self.before | {"capabilities": {"can_move": True}}
        proof = self.read_proof(self.before) | {"snapshot_ref": self.journal.evidence(changed)}
        with self.assertRaisesRegex(SafetyError, "OPTIONAL_FIELD_UNPROVEN"):
            self.bridge._agent_read(changed, proof)

    def test_completed_receipt_with_omitted_iserror_requires_independent_postread(self):
        envelope = self.prepare()
        self.mark(envelope)
        terminal = self.completed(envelope)
        self.assertEqual(self.journal.records()[-1]["kind"], "UNCERTAIN")
        observed, parents = self.after()
        result = self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check(observed, parents), terminal)
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")

    def test_explicit_error_is_not_terminal_success(self):
        envelope = self.prepare()
        self.mark(envelope)
        terminal = self.completed(envelope, error=True)
        observed, parents = self.after()
        with self.assertRaisesRegex(SafetyError, "UNPROVEN"):
            self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check(observed, parents), terminal)
        self.assertEqual(self.journal.records()[-1]["kind"], "UNCERTAIN")

    def test_timeout_preimage_cannot_authorize_retry(self):
        envelope = self.prepare()
        self.mark(envelope)
        terminal = {"kind": "AGENT_UNRESOLVED", "mode": "AGENT_ASSISTED", "terminal": False,
                    "attempt_key": envelope["attempt_key"], "envelope_hash": envelope["envelope_hash"]}
        result = self.bridge.reconcile_action(envelope["envelope_hash"], self.before, self.check(), terminal)
        self.assertEqual(result["kind"], "UNCERTAIN")
        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
            self.mark(envelope)

    def test_known_parent_permission_drift_conflicts(self):
        envelope = self.prepare()
        self.mark(envelope)
        terminal = self.completed(envelope)
        observed, parents = self.after()
        parents["agent-fixture-target"]["permissions"].append({"id": "unreviewed", "role": "reader"})
        result = self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check(observed, parents), terminal)
        self.assertEqual(result["kind"], "BLOCKED_CONFLICT")

    def test_pause_still_accepts_late_reconciliation(self):
        envelope = self.prepare()
        self.mark(envelope)
        self.bridge.disable_agent_dispatch()
        terminal = self.completed(envelope)
        observed, parents = self.after()
        receiver = ActionBridge(Journal(self.journal.root))
        result = receiver.reconcile_action(envelope["envelope_hash"], observed, self.check(observed, parents), terminal)
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")

    def test_crash_after_agent_attempt_does_not_reissue_ticket(self):
        envelope = self.prepare()
        def crash(point):
            if point == "after_ATTEMPTED":
                raise ProcessDeath()
        self.bridge.fault = crash
        with self.assertRaises(ProcessDeath):
            self.mark(envelope)
        self.bridge = ActionBridge(Journal(self.journal.root), "AGENT_ASSISTED", controller_id="synthetic-controller")
        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
            self.mark(envelope)

    def test_known_raw_fields_cannot_be_hidden_as_unknown(self):
        proof = self.read_proof(self.before)
        for changed in ({k: v for k, v in self.before.items() if k != "permissions"}, self.before | {"permissions": None}):
            with self.subTest(changed=changed), self.assertRaisesRegex(SafetyError, "FIELD_DROPPED"):
                self.bridge._agent_read(changed, proof | {"snapshot_ref": self.journal.evidence(self.snapshot(changed))})

    def test_approved_access_baseline_blocks_new_audience_before_prepare_and_mark(self):
        self.authorize()
        parents = copy.deepcopy(self.parents)
        parents["agent-fixture-target"]["permissions"].append({"id": "unreviewed", "role": "reader"})
        with self.assertRaisesRegex(SafetyError, "APPROVED_ACCESS_BASELINE_DRIFT"):
            self.bridge.prepare_action(self.plan, self.plan.operations[0].operation_id, self.approval, self.before, self.check(parents=parents))
        envelope = self.prepare()
        with self.assertRaisesRegex(SafetyError, "APPROVED_ACCESS_BASELINE_DRIFT"):
            self.bridge.mark_attempt(envelope["envelope_hash"], self.before, self.check(parents=parents))
        self.assertFalse(any(r["kind"] == "ATTEMPTED" for r in self.journal.records()))

    def test_access_baselines_are_required_and_unknown_cannot_hide_new_known_grants(self):
        exposure = self.journal.read_evidence(self.auth["exposure_review_ref"])
        exposure.pop("access_baselines")
        auth = self.auth | {"exposure_review_ref": self.journal.evidence(exposure)}
        with self.assertRaisesRegex(SafetyError, "ACCESS_BASELINES_REQUIRED"):
            self.bridge.authorize_agent_dispatch(self.plan, self.approval, auth, self.enable | {"authorization_hash": digest(auth)})
        self.authorize()
        parents = copy.deepcopy(self.parents)
        parents["agent-fixture-target"]["access"] = {"complete": False, "principals": ["new-known-audience"]}
        with self.assertRaisesRegex(SafetyError, "ACCESS_BASELINE_DRIFT"):
            self.bridge.prepare_action(self.plan, self.plan.operations[0].operation_id, self.approval, self.before, self.check(parents=parents))

    def test_completed_success_with_preimage_remains_conflict_and_fenced(self):
        envelope = self.prepare()
        self.mark(envelope)
        terminal = self.completed(envelope)
        result = self.bridge.reconcile_action(envelope["envelope_hash"], self.before, self.check(), terminal)
        self.assertEqual(result["kind"], "BLOCKED_CONFLICT")
        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
            self.mark(envelope)

    def test_direct_grant_change_after_move_is_conflict(self):
        envelope = self.prepare()
        self.mark(envelope)
        terminal = self.completed(envelope)
        observed, parents = self.after()
        observed["permissions"] = []
        result = self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check(observed, parents), terminal)
        self.assertEqual(result["kind"], "BLOCKED_CONFLICT")

    def test_reprepare_fresh_receipts_is_idempotent_and_pause_blocks_mark(self):
        envelope = self.prepare()
        again = self.bridge.prepare_action(self.plan, self.plan.operations[0].operation_id, self.approval, self.before, self.check())
        self.assertEqual(envelope, again)
        self.bridge.disable_agent_dispatch()
        with self.assertRaisesRegex(SafetyError, "NOT_ENABLED"):
            self.mark(envelope)

    def test_agent_rename_then_move_refreshes_only_verified_version(self):
        rename = Operation("agent-two:rename", self.before["id"], "RENAME", self.before, {"name": "Approved.pdf"})
        renamed = self.snapshot(rename.intended(self.before))
        move = Operation("agent-two:move", self.before["id"], "MOVE", renamed,
                         {"parents": ["agent-fixture-target"]}, (rename.operation_id,))
        self.configure([rename, move])
        self.authorize()
        envelope = self.bridge.prepare_action(self.plan, rename.operation_id, self.approval, self.before, self.check(op=rename))
        ticket = self.bridge.mark_attempt(envelope["envelope_hash"], self.before, self.check(op=rename))
        self.assertEqual(ticket["args"], {"fileId": self.before["id"], "name": "Approved.pdf"})
        terminal = self.completed(envelope)
        renamed = copy.deepcopy(renamed)
        renamed["version"] = "provider-version-after-rename"
        result = self.bridge.reconcile_action(envelope["envelope_hash"], renamed, self.check(renamed, op=rename), terminal)
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")
        envelope = self.bridge.prepare_action(self.plan, move.operation_id, self.approval, renamed, self.check(before=renamed, op=move))
        ticket = self.bridge.mark_attempt(envelope["envelope_hash"], renamed, self.check(before=renamed, op=move))
        self.assertEqual(set(ticket["args"]), {"fileId", "addParents", "removeParents"})

    def test_folder_move_uses_explicit_direct_parent_membership(self):
        folder = self.snapshot(self.before | {"mime_type": FOLDER, "name": "Folder",
            "subtree": {"complete": True, "membership_scope": "DESCENDANTS", "members": [{"id": "agent-fixture-child", "name": "Child.pdf"}]}})
        move = Operation("agent-folder:move", folder["id"], "MOVE", folder, {"parents": ["agent-fixture-target"]})
        self.configure([move])
        self.authorize()
        envelope = self.bridge.prepare_action(self.plan, move.operation_id, self.approval, folder, self.check(before=folder))
        self.bridge.mark_attempt(envelope["envelope_hash"], folder, self.check(before=folder))
        terminal = self.completed(envelope)
        observed = self.snapshot(move.intended(folder))
        parents = copy.deepcopy(self.parents)
        parents["agent-fixture-origin"]["subtree"]["members"] = []
        parents["agent-fixture-target"]["subtree"]["members"] = [{"id": folder["id"]}]
        result = self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check(observed, parents, folder), terminal)
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")

    def test_parent_membership_scope_cannot_be_assumed(self):
        self.authorize()
        parents = copy.deepcopy(self.parents)
        parents["agent-fixture-target"]["subtree"].pop("membership_scope")
        with self.assertRaisesRegex(SafetyError, "MEMBERSHIP_SCOPE_REQUIRED"):
            self.bridge.prepare_action(self.plan, self.plan.operations[0].operation_id, self.approval, self.before, self.check(parents=parents))

    def test_proven_nondispatch_releases_fence_but_never_reissues_ticket(self):
        envelope = self.prepare()
        self.mark(envelope)
        terminal = self.refused(envelope)
        result = self.bridge.reconcile_action(envelope["envelope_hash"], self.before, self.check(), terminal)
        self.assertEqual(result["kind"], "FAILED_NOT_APPLIED")
        self.assertEqual(result["payload"]["reason_code"], "TRUSTED_NONDISPATCH_AND_EXACT_PREIMAGE")
        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
            self.mark(envelope)

    def test_generic_error_cannot_be_relabelled_as_nondispatch(self):
        envelope = self.prepare()
        self.mark(envelope)
        terminal = self.refused(envelope, definitive=False)
        with self.assertRaisesRegex(SafetyError, "NONDISPATCH_UNPROVEN"):
            self.bridge.reconcile_action(envelope["envelope_hash"], self.before, self.check(), terminal)
        self.assertEqual(self.journal.records()[-1]["kind"], "UNCERTAIN")

    def test_independent_second_ticket_waits_for_first_reconciliation(self):
        other = self.snapshot(self.before | {"id": "agent-fixture-other", "parents": ["agent-fixture-origin-two"]})
        parents = copy.deepcopy(self.parents)
        for name, members in (("agent-fixture-origin-two", [{"id": other["id"]}]), ("agent-fixture-target-two", [])):
            parents[name] = self.snapshot(self.parents["agent-fixture-origin"] | {"id": name, "name": name,
                "subtree": {"complete": True, "membership_scope": "DIRECT_CHILDREN", "members": members}})
        first = replace(self.plan.operations[0], operation_id="agent-two:first")
        second = Operation("agent-two:second", other["id"], "MOVE", other, {"parents": ["agent-fixture-target-two"]})
        self.configure([first, second], parents)
        self.authorize()
        a = self.bridge.prepare_action(self.plan, first.operation_id, self.approval, self.before, self.check(op=first))
        b = self.bridge.prepare_action(self.plan, second.operation_id, self.approval, other, self.check(before=other, op=second))
        self.bridge.mark_attempt(a["envelope_hash"], self.before, self.check(op=first))
        with self.assertRaisesRegex(SafetyError, "OTHER_DISPATCH_UNRESOLVED"):
            self.bridge.mark_attempt(b["envelope_hash"], other, self.check(before=other, op=second))

    def test_named_descendants_refresh_before_same_folder_dependency_compare(self):
        root = self.snapshot(self.before | {"mime_type": FOLDER, "name": "Root",
            "subtree": {"complete": True, "membership_scope": "DESCENDANTS", "members": [{"id": "agent-fixture-child", "name": "Original.pdf"}]}})
        root_rename = Operation("agent-tree:root-rename", root["id"], "RENAME", root, {"name": "Approved Root"})
        renamed_root = self.snapshot(root_rename.intended(root))
        child = self.snapshot(self.before | {"id": "agent-fixture-child", "parents": [root["id"]]})
        child_rename = Operation("agent-tree:child-rename", child["id"], "RENAME", child, {"name": "Approved.pdf"}, (root_rename.operation_id,))
        final_root = copy.deepcopy(renamed_root)
        final_root["subtree"]["members"][0]["name"] = "Approved.pdf"
        root_move = Operation("agent-tree:move", root["id"], "MOVE", final_root, {"parents": ["agent-fixture-target"]}, (child_rename.operation_id,))
        parents = self.parents | {root["id"]: root}
        self.configure([root_rename, child_rename, root_move], parents)
        self.authorize()
        # These are synthetic verified dependency receipts, with complete frozen
        # identities and exact images, not a live connector result.
        for op, observed in ((root_rename, renamed_root | {"version": "root-after-rename"}),
                             (child_rename, child_rename.intended(child))):
            self.journal.append("RECONCILED_AT_TARGET", op.operation_id, payload={"file_id": op.file_id,
                "action": op.action, "direction": "FORWARD", "depends_on": list(op.depends_on),
                "before_ref": self.journal.evidence(op.before), "observed_ref": self.journal.evidence(observed)})
        final_root = copy.deepcopy(final_root)
        final_root["version"] = "root-after-rename"
        envelope = self.bridge.prepare_action(self.plan, root_move.operation_id, self.approval, final_root,
                                              self.check(before=final_root, op=root_move))
        self.assertEqual(envelope["action"], "MOVE")

    def test_named_unrelated_members_cannot_drift_during_action(self):
        parents = copy.deepcopy(self.parents)
        parents["agent-fixture-origin"]["subtree"]["members"].append({"id": "agent-fixture-sibling", "name": "Keep.pdf"})
        self.parents = parents
        envelope = self.prepare()
        self.mark(envelope)
        terminal = self.completed(envelope)
        observed, after = self.after()
        after["agent-fixture-origin"]["subtree"]["members"] = [{"id": "agent-fixture-sibling", "name": "Manual.pdf"}]
        result = self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check(observed, after), terminal)
        self.assertEqual(result["kind"], "BLOCKED_CONFLICT")

    def test_named_child_manual_rename_after_folder_move_is_conflict(self):
        folder = self.snapshot(self.before | {"mime_type": FOLDER, "name": "Folder",
            "subtree": {"complete": True, "membership_scope": "DESCENDANTS",
                        "members": [{"id": "agent-fixture-child", "name": "Keep.pdf", "mime_type": "application/pdf"}]}})
        move = Operation("agent-folder:move", folder["id"], "MOVE", folder, {"parents": ["agent-fixture-target"]})
        self.configure([move])
        self.authorize()
        envelope = self.bridge.prepare_action(self.plan, move.operation_id, self.approval, folder, self.check(before=folder))
        self.bridge.mark_attempt(envelope["envelope_hash"], folder, self.check(before=folder))
        terminal = self.completed(envelope)
        observed = self.snapshot(move.intended(folder))
        observed["subtree"]["members"][0]["name"] = "Manual.pdf"
        parents = copy.deepcopy(self.parents)
        parents["agent-fixture-origin"]["subtree"]["members"] = []
        parents["agent-fixture-target"]["subtree"]["members"] = [{"id": folder["id"]}]
        result = self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check(observed, parents, folder), terminal)
        self.assertEqual(result["kind"], "BLOCKED_CONFLICT")

    def test_approved_parent_access_refresh_requires_verified_dependency(self):
        root = self.snapshot(self.before | {"mime_type": FOLDER, "name": "Root",
            "subtree": {"complete": True, "membership_scope": "DESCENDANTS", "members": []}})
        incoming = self.snapshot(self.before | {"id": "agent-fixture-incoming"})
        root_move = Operation("agent-root:move", root["id"], "MOVE", root, {"parents": ["agent-fixture-target"]})
        incoming_move = Operation("agent-root:incoming", incoming["id"], "MOVE", incoming,
                                 {"parents": [root["id"]]}, (root_move.operation_id,))
        parents = copy.deepcopy(self.parents) | {root["id"]: root}
        parents["agent-fixture-origin"]["subtree"]["members"].append({"id": incoming["id"]})
        self.configure([root_move, incoming_move], parents)
        self.authorize()
        envelope = self.bridge.prepare_action(self.plan, root_move.operation_id, self.approval, root, self.check(before=root, op=root_move))
        self.bridge.mark_attempt(envelope["envelope_hash"], root, self.check(before=root, op=root_move))
        terminal = self.completed(envelope)
        moved_root = self.snapshot(root_move.intended(root))
        moved_root["permissions"].append({"id": "known-user", "role": "writer", "permissionDetails": [{"inherited": True}]})
        after_parents = copy.deepcopy(parents)
        after_parents["agent-fixture-origin"]["subtree"]["members"] = [{"id": incoming["id"]}]
        after_parents["agent-fixture-target"]["subtree"]["members"] = [{"id": root["id"]}]
        result = self.bridge.reconcile_action(envelope["envelope_hash"], moved_root,
            self.check(moved_root, after_parents, root, root_move), terminal)
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")
        after_parents[root["id"]] = moved_root
        following = self.bridge.prepare_action(self.plan, incoming_move.operation_id, self.approval, incoming,
            self.check(before=incoming, parents=after_parents, op=incoming_move))
        self.assertEqual(following["file_id"], incoming["id"])
        after_parents = copy.deepcopy(after_parents)
        after_parents[root["id"]]["permissions"].append({"id": "unreviewed", "role": "reader"})
        with self.assertRaisesRegex(SafetyError, "ACCESS_BASELINE_DRIFT"):
            self.bridge.mark_attempt(following["envelope_hash"], incoming,
                self.check(before=incoming, parents=after_parents, op=incoming_move))

    def test_agent_authority_transition_crash_matrix(self):
        for kind in ("APPROVED", "HANDOFF_AUTHORIZED", "HANDOFF_DISPATCH_ENABLED"):
            for side in ("before_", "after_"):
                with self.subTest(point=side + kind):
                    self.tearDown()
                    self.setUp()
                    def crash(point):
                        if point == side + kind:
                            raise ProcessDeath()
                    self.bridge.fault = crash
                    with self.assertRaises(ProcessDeath):
                        self.authorize()
                    self.bridge = ActionBridge(Journal(self.journal.root), "AGENT_ASSISTED", controller_id="synthetic-controller")
                    self.authorize()
                    for event in ("APPROVED", "HANDOFF_AUTHORIZED", "HANDOFF_DISPATCH_ENABLED"):
                        self.assertEqual(sum(r["kind"] == event for r in self.journal.records()), 1)
                    envelope = self.prepare()
                    self.mark(envelope)
                    self.assertEqual(sum(r["kind"] == "ATTEMPTED" for r in self.journal.records()), 1)

    def test_agent_action_transition_crash_matrix(self):
        for kind in ("PREPARED", "ATTEMPTED", "HANDOFF_RESULT_RECEIVED", "UNCERTAIN",
                     "RECONCILED_AT_TARGET", "FAILED_NOT_APPLIED", "BLOCKED_CONFLICT"):
            for side in ("before_", "after_"):
                with self.subTest(point=side + kind):
                    self.tearDown()
                    self.setUp()
                    self.authorize()
                    envelope = None if kind == "PREPARED" else self.prepare()
                    if kind not in {"PREPARED", "ATTEMPTED"}:
                        self.mark(envelope)
                    terminal = None
                    if kind in {"RECONCILED_AT_TARGET", "BLOCKED_CONFLICT", "FAILED_NOT_APPLIED"}:
                        terminal = self.refused(envelope) if kind == "FAILED_NOT_APPLIED" else self.completed(envelope)
                    def crash(point):
                        if point == side + kind:
                            raise ProcessDeath()
                    self.bridge.fault = crash
                    def transition():
                        if kind == "PREPARED":
                            return self.prepare()
                        if kind == "ATTEMPTED":
                            return self.mark(envelope)
                        if kind in {"HANDOFF_RESULT_RECEIVED", "UNCERTAIN"}:
                            return self.bridge.ingest_result(envelope["envelope_hash"], envelope["attempt_key"], {"structuredContent": {"id": envelope["file_id"]}})
                        observed, parents = self.after() if kind == "RECONCILED_AT_TARGET" else (self.before, self.parents)
                        return self.bridge.reconcile_action(envelope["envelope_hash"], observed, self.check(observed, parents), terminal)
                    with self.assertRaises(ProcessDeath):
                        transition()
                    self.bridge = ActionBridge(Journal(self.journal.root), "AGENT_ASSISTED", controller_id="synthetic-controller")
                    if kind == "ATTEMPTED" and side == "after_":
                        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
                            transition()
                    else:
                        result = transition()
                        if kind == "PREPARED":
                            envelope = result
                            self.mark(envelope)
                    self.assertEqual(sum(r["kind"] == "ATTEMPTED" for r in self.journal.records()), 1)
                    if kind not in {"PREPARED", "ATTEMPTED"}:
                        with self.assertRaisesRegex(SafetyError, "CONSUMED"):
                            self.mark(envelope)


if __name__ == "__main__":
    unittest.main()
