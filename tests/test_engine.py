"""Safety tests: observable behavior, durable recovery and explicit action fences."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from uqmc_recovery.adapters import MockAdapter, fixture_state
from uqmc_recovery.engine import Engine
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import Approval, FOLDER, Operation, Plan, SafetyError, WITHDRAWN_ID, digest


class ProcessDeath(BaseException):
    """Not caught as an ordinary remote exception: models abrupt process loss."""


def approval(plan, **changes):
    clock = datetime.now(timezone.utc)
    fields = dict(
        approval_id=plan.batch_id + ":approval", run_id=plan.run_id,
        batch_id=plan.batch_id, proposal_revision=plan.proposal_revision,
        proposal_hash=plan.proposal_hash,
        operation_ids=tuple(op.operation_id for op in plan.operations),
        actor="fixture-user", approved_at=(clock - timedelta(minutes=1)).isoformat(),
        expires_at=(clock + timedelta(days=1)).isoformat(), mode="TEST_ONLY",
    )
    fields.update(changes)
    return Approval(**fields)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal"
        self.journal = Journal(self.path)
        self.original = fixture_state("doc", "Budget.pdf")
        self.adapter = MockAdapter({
            "doc": self.original,
            "origin": fixture_state("origin", "Original folder", FOLDER, ()),
            "destination": fixture_state("destination", "Destination folder", FOLDER, ()),
        })
        self.engine = Engine(self.journal, self.adapter)

    def tearDown(self):
        self.temp.cleanup()

    def rename_plan(self, batch="batch", file_id="doc", target="Renamed.pdf", before=None):
        return Plan(batch, batch, "revision-1", (
            Operation(batch + ":rename", file_id, "RENAME", copy.deepcopy(before or self.original), {"name": target}),
        ))

    def compound_plan(self, batch="batch"):
        move = Operation(batch + ":move", "doc", "MOVE", copy.deepcopy(self.original), {"parents": ["destination"]})
        rename = Operation(batch + ":rename", "doc", "RENAME", move.intended(), {"name": "Renamed.pdf"}, (move.operation_id,))
        return Plan(batch, batch, "revision-1", (move, rename))

    def test_move_and_rename_have_independent_verified_evidence(self):
        plan = self.compound_plan()
        result = self.engine.run(plan, approval(plan))
        self.assertEqual(set(result.values()), {"VERIFIED_APPLIED"})
        self.assertEqual(self.adapter.states["doc"]["name"], "Renamed.pdf")
        self.assertEqual(self.adapter.states["doc"]["parents"], ["destination"])
        self.assertEqual(len(self.adapter.calls), 2)
        prepared = [r for r in self.journal.records() if r["kind"] == "PREPARED"]
        self.assertEqual([r["payload"]["action"] for r in prepared], ["MOVE", "RENAME"])
        first = self.journal.read_evidence(prepared[0]["payload"]["before_ref"])
        self.assertEqual(first, self.original)
        self.assertEqual(first["permissions"], self.original["permissions"])
        self.assertTrue(all(r["payload"]["approval_id"] == plan.batch_id + ":approval" for r in prepared))

    def test_resume_is_idempotent_and_never_duplicates_dispatch(self):
        plan = self.compound_plan()
        auth = approval(plan)
        first = self.engine.run(plan, auth)
        saved = len(self.journal.records())
        reopened = Engine(Journal(self.path), self.adapter)
        self.assertEqual(reopened.run(plan, auth), first)
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertEqual(len(self.journal.records()), saved)

    def test_dry_run_has_no_dispatch_or_execution_approval(self):
        plan = self.rename_plan()
        before = copy.deepcopy(self.adapter.states)
        result = self.engine.dry_run(plan)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["live_execution_enabled"])
        self.assertEqual(result["results"][0]["state"], "READ_ONLY_READY")
        self.assertEqual(self.adapter.states, before)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.journal.records(), [])

    def test_rollback_reverses_separate_actions_and_retains_folders(self):
        plan = self.compound_plan()
        self.engine.run(plan, approval(plan))
        inverse = self.engine.rollback_plan(plan, [op.operation_id for op in plan.operations], "rollback")
        self.assertEqual([op.action for op in inverse.operations], ["RENAME", "MOVE"])
        self.assertEqual([op.original_operation_id for op in inverse.operations], [op.operation_id for op in reversed(plan.operations)])
        self.assertEqual(set(self.engine.run(inverse, approval(inverse)).values()), {"VERIFIED_APPLIED"})
        self.assertEqual(self.adapter.states["doc"]["name"], self.original["name"])
        self.assertEqual(self.adapter.states["doc"]["parents"], self.original["parents"])
        self.assertIn("origin", self.adapter.states)
        self.assertIn("destination", self.adapter.states)
        self.assertEqual(self.engine.rollback_plan(plan, [op.operation_id for op in plan.operations], "again").operations, ())

    def test_rollback_refuses_to_overwrite_manual_name_parent_or_permission_changes(self):
        for kind in ("name", "parents", "permissions", "version"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                adapter = MockAdapter(copy.deepcopy(self.adapter.states))
                engine = Engine(Journal(Path(temp) / "journal"), adapter)
                plan = self.rename_plan()
                engine.run(plan, approval(plan))
                inverse = engine.rollback_plan(plan, [plan.operations[0].operation_id], "undo")
                if kind == "name":
                    adapter.states["doc"][kind] = "Manual.pdf"
                elif kind == "parents":
                    adapter.states["doc"][kind] = ["destination"]
                elif kind == "permissions":
                    adapter.states["doc"][kind].append({"id": "manual-share", "type": "user", "role": "reader"})
                else:
                    adapter.states["doc"][kind] = "999"
                manual = copy.deepcopy(adapter.states["doc"])
                result = engine.run(inverse, approval(inverse))
                self.assertEqual(set(result.values()), {"BLOCKED_CONFLICT"})
                self.assertEqual(adapter.states["doc"], manual)
                self.assertEqual(len(adapter.calls), 1)

    def test_partial_execution_failure_leaves_success_and_dependency_wait_distinct(self):
        plan = self.compound_plan()
        auth = approval(plan)
        crashed = []
        def stop_after_move(point):
            if point == "after_VERIFIED_APPLIED" and not crashed:
                crashed.append(True)
                raise ProcessDeath(point)
        self.engine.fault = stop_after_move
        with self.assertRaises(ProcessDeath):
            self.engine.run(plan, auth)
        self.assertEqual(self.engine.latest(plan.operations[0].operation_id)["kind"], "VERIFIED_APPLIED")
        self.assertIsNone(self.engine.latest(plan.operations[1].operation_id))
        self.assertEqual(self.adapter.states["doc"]["name"], self.original["name"])
        self.assertEqual(self.adapter.states["doc"]["parents"], ["destination"])
        reopened = Engine(Journal(self.path), self.adapter)
        self.assertEqual(set(reopened.run(plan, auth).values()), {"VERIFIED_APPLIED"})
        self.assertEqual(len(self.adapter.calls), 2)

    def test_crash_injection_before_and_after_every_success_transition(self):
        points = [stage + kind for kind in ("DRAFTED", "APPROVED", "PREPARED", "ATTEMPTED", "VERIFIED_APPLIED") for stage in ("before_", "after_")]
        points += ["before_final_read", "after_final_read", "before_remote_apply", "after_remote_apply", "after_response_evidence", "after_readback", "after_postflight"]
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temp:
                adapter = MockAdapter(copy.deepcopy(self.adapter.states))
                journal = Journal(Path(temp) / "journal")
                visited = []
                def fail_at(actual):
                    if actual == point and not visited:
                        visited.append(actual)
                        raise ProcessDeath(actual)
                engine = Engine(journal, adapter, fail_at)
                plan = self.rename_plan()
                auth = approval(plan)
                with self.assertRaises(ProcessDeath):
                    engine.run(plan, auth)
                before_restart_calls = len(adapter.calls)
                restored = Engine(Journal(journal.root), adapter)
                restored.run(plan, auth)
                restored.run(plan, auth)
                self.assertLessEqual(len(adapter.calls), 1, point)
                self.assertEqual(visited, [point])
                latest = restored.latest(plan.operations[0].operation_id)
                self.assertIn(latest["kind"], {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET", "UNCERTAIN", "FAILED_NOT_APPLIED"})
                if before_restart_calls:
                    self.assertEqual(len(adapter.calls), before_restart_calls)
                if adapter.states["doc"]["name"] == "Renamed.pdf":
                    self.assertIn(latest["kind"], {"VERIFIED_APPLIED", "RECONCILED_AT_TARGET"})

    def test_remote_success_before_journal_commit_is_reconciled_without_reapply(self):
        plan = self.rename_plan()
        auth = approval(plan)
        def crash(point):
            if point == "after_remote_apply":
                raise ProcessDeath()
        self.engine.fault = crash
        with self.assertRaises(ProcessDeath):
            self.engine.run(plan, auth)
        self.assertEqual(self.engine.latest(plan.operations[0].operation_id)["kind"], "ATTEMPTED")
        result = Engine(Journal(self.path), self.adapter).run(plan, auth)
        self.assertEqual(set(result.values()), {"RECONCILED_AT_TARGET"})
        self.assertEqual(len(self.adapter.calls), 1)
        last = self.engine.latest(plan.operations[0].operation_id)
        self.assertEqual(last["payload"]["reason_code"], "OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN")

    def test_crash_injection_before_and_after_non_success_transitions(self):
        for kind in ("SKIPPED", "BLOCKED_CONFLICT", "UNCERTAIN", "FAILED_NOT_APPLIED", "RECONCILED_AT_TARGET"):
            for side in ("before_", "after_"):
                point = side + kind
                with self.subTest(point=point), tempfile.TemporaryDirectory() as temp:
                    adapter = MockAdapter(copy.deepcopy(self.adapter.states))
                    journal = Journal(Path(temp) / "journal")
                    engine = Engine(journal, adapter)
                    plan = self.rename_plan(target="Budget.pdf" if kind == "SKIPPED" else "Renamed.pdf")
                    auth = approval(plan)
                    if kind == "BLOCKED_CONFLICT":
                        adapter.states["doc"]["name"] = "Manual.pdf"
                    if kind in {"UNCERTAIN", "RECONCILED_AT_TARGET", "FAILED_NOT_APPLIED"}:
                        adapter.failure = "before_apply" if kind == "FAILED_NOT_APPLIED" else "after_apply"
                    if kind in {"FAILED_NOT_APPLIED", "RECONCILED_AT_TARGET"}:
                        engine.run(plan, auth)
                    visited = []
                    def crash(actual):
                        if actual == point:
                            visited.append(actual)
                            raise ProcessDeath(actual)
                    engine.fault = crash
                    with self.assertRaises(ProcessDeath):
                        engine.run(plan, auth)
                    engine = Engine(Journal(journal.root), adapter)
                    outcome = engine.run(plan, auth)
                    self.assertEqual(visited, [point])
                    self.assertLessEqual(len(adapter.calls), 1)
                    self.assertEqual(set(outcome.values()), {"RECONCILED_AT_TARGET" if kind == "UNCERTAIN" else kind})

    def test_crash_after_reconciliation_read_does_not_authorize_retry(self):
        plan = self.rename_plan()
        auth = approval(plan)
        self.adapter.failure = "after_apply"
        self.engine.run(plan, auth)
        def crash(point):
            if point == "after_reconcile_read":
                raise ProcessDeath()
        self.engine.fault = crash
        with self.assertRaises(ProcessDeath):
            self.engine.run(plan, auth)
        engine = Engine(Journal(self.path), self.adapter)
        self.assertEqual(set(engine.run(plan, auth).values()), {"RECONCILED_AT_TARGET"})
        self.assertEqual(len(self.adapter.calls), 1)

    def test_uncertain_delayed_request_blocks_dependents_and_same_batch_retry(self):
        plan = self.compound_plan()
        auth = approval(plan)
        self.adapter.failure = "pending"
        result = self.engine.run(plan, auth)
        self.assertEqual(result[plan.operations[0].operation_id], "UNCERTAIN")
        self.assertEqual(result[plan.operations[1].operation_id], "NOT_ATTEMPTED")
        calls = len(self.adapter.calls)
        self.adapter.failure = None
        for _ in range(3):
            self.engine.run(plan, auth)
        self.assertEqual(len(self.adapter.calls), calls)
        self.assertEqual(self.engine.latest(plan.operations[0].operation_id)["kind"], "UNCERTAIN")
        with self.assertRaisesRegex(SafetyError, "RECONCILE_BEFORE_ROLLBACK"):
            self.engine.rollback_plan(plan, [plan.operations[0].operation_id], "undo")

    def test_new_batch_cannot_bypass_an_unresolved_prior_request(self):
        plan = self.rename_plan()
        self.adapter.failure = "pending"
        self.engine.run(plan, approval(plan))
        calls = len(self.adapter.calls)
        self.adapter.failure = None
        replacement = self.rename_plan("new-batch")
        try:
            self.engine.run(replacement, approval(replacement))
        except SafetyError:
            pass
        self.assertEqual(len(self.adapter.calls), calls)
        self.assertEqual(self.adapter.states["doc"]["name"], self.original["name"])

    def test_readback_failure_and_timeout_after_success_reconcile(self):
        for failure in ("after_apply", "readback"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                adapter = MockAdapter(copy.deepcopy(self.adapter.states))
                engine = Engine(Journal(Path(temp) / "journal"), adapter)
                plan = self.rename_plan()
                auth = approval(plan)
                adapter.failure = failure
                result = engine.run(plan, auth)
                self.assertEqual(set(result.values()), {"UNCERTAIN"})
                adapter.failure = None
                result = engine.run(plan, auth)
                self.assertEqual(set(result.values()), {"RECONCILED_AT_TARGET"})
                self.assertEqual(len(adapter.calls), 1)

    def test_terminal_failure_is_recorded_and_same_batch_never_retries(self):
        plan = self.rename_plan()
        auth = approval(plan)
        self.adapter.failure = "before_apply"
        self.engine.run(plan, auth)
        self.adapter.failure = None
        result = self.engine.run(plan, auth)
        self.assertEqual(set(result.values()), {"FAILED_NOT_APPLIED"})
        self.engine.run(plan, auth)
        self.assertEqual(len(self.adapter.calls), 1)

    def test_reconciliation_third_state_blocks(self):
        plan = self.rename_plan()
        auth = approval(plan)
        self.adapter.failure = "after_apply"
        self.engine.run(plan, auth)
        self.adapter.failure = None
        self.adapter.states["doc"]["name"] = "Subsequent manual name.pdf"
        result = self.engine.run(plan, auth)
        self.assertEqual(set(result.values()), {"BLOCKED_CONFLICT"})
        self.assertEqual(len(self.adapter.calls), 1)

    def test_baseline_drift_blocks_before_dispatch(self):
        plan = self.rename_plan()
        self.adapter.states["doc"]["name"] = "Manual.pdf"
        result = self.engine.run(plan, approval(plan))
        self.assertEqual(set(result.values()), {"BLOCKED_CONFLICT"})
        self.assertEqual(self.adapter.calls, [])

    def test_access_change_needs_separate_exact_hash_approval(self):
        folder = fixture_state("folder", "Club", FOLDER)
        self.adapter.states["folder"] = copy.deepcopy(folder)
        self.adapter.states["destination"]["access"]["effective"].append("fixture-extra-reader")
        operation = Operation("move-folder", "folder", "MOVE", folder, {"parents": ["destination"]})
        plan = Plan("batch", "batch", "r1", (operation,))
        dry = self.engine.dry_run(plan)
        self.assertEqual(dry["results"][0]["reason_code"], "ACCESS_CHANGE_REQUIRES_EXPLICIT_APPROVAL")
        check = self.adapter.preflight(operation, folder, set())
        expected = check["expected_after"]
        access_hash = digest({"before": {k: folder[k] for k in ("permissions", "access", "subtree")},
                              "after": {k: expected[k] for k in ("permissions", "access", "subtree")}})
        result = self.engine.run(plan, approval(plan, access_change_hashes={operation.operation_id: access_hash}))
        self.assertEqual(set(result.values()), {"VERIFIED_APPLIED"})
        self.assertIn("origin", self.adapter.states)
        self.assertEqual(self.adapter.states["folder"]["access"], expected["access"])

    def test_incomplete_effective_access_or_descendant_inventory_fails_closed(self):
        for field in ("access", "subtree"):
            with self.subTest(field=field):
                folder = fixture_state("folder", "Club", FOLDER)
                folder[field]["complete"] = False
                operation = Operation("move", "folder", "MOVE", folder, {"parents": ["destination"]})
                plan = Plan("batch", "batch", "r1", (operation,))
                with self.assertRaises(SafetyError):
                    self.engine.dry_run(plan)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.adapter.read_calls, [])

    def test_destination_collision_blocks_without_overwrite(self):
        self.adapter.states["other"] = fixture_state("other", "Renamed.pdf")
        plan = self.rename_plan()
        result = self.engine.run(plan, approval(plan))
        self.assertEqual(set(result.values()), {"BLOCKED_CONFLICT"})
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.adapter.states["other"]["name"], "Renamed.pdf")
        self.assertEqual(self.adapter.states["doc"]["name"], "Budget.pdf")

    def test_folder_move_into_descendant_is_blocked(self):
        folder = fixture_state("folder", "Club", FOLDER)
        folder["subtree"]["members"] = [{"id": "destination"}]
        self.adapter.states["folder"] = copy.deepcopy(folder)
        operation = Operation("move", "folder", "MOVE", folder, {"parents": ["destination"]})
        plan = Plan("batch", "batch", "r1", (operation,))
        result = self.engine.run(plan, approval(plan))
        self.assertEqual(set(result.values()), {"BLOCKED_CONFLICT"})
        self.assertEqual(self.adapter.calls, [])

    def test_last_moment_drift_is_blocked_before_dispatch(self):
        plan = self.rename_plan()
        def manual_change(point):
            if point == "after_PREPARED":
                self.adapter.states["doc"]["name"] = "Manual.pdf"
        self.engine.fault = manual_change
        result = self.engine.run(plan, approval(plan))
        self.assertEqual(set(result.values()), {"BLOCKED_CONFLICT"})
        self.assertEqual(self.adapter.calls, [])

    def test_mock_conditional_update_blocks_race_after_final_read(self):
        plan = self.rename_plan()
        auth = approval(plan)
        def manual_change(point):
            if point == "after_final_read":
                self.adapter.states["doc"]["name"] = "Manual.pdf"
        self.engine.fault = manual_change
        self.engine.run(plan, auth)
        self.engine.fault = lambda point: None
        result = self.engine.run(plan, auth)
        self.assertEqual(set(result.values()), {"BLOCKED_CONFLICT"})
        self.assertEqual(self.adapter.states["doc"]["name"], "Manual.pdf")
        self.assertEqual(len(self.adapter.calls), 1)

    def test_stale_scope_expired_revoked_or_live_approval_is_rejected(self):
        plan = self.rename_plan()
        cases = [
            {"proposal_hash": "0" * 64}, {"proposal_revision": "old"},
            {"batch_id": "other"}, {"operation_ids": ()},
            {"operation_ids": (plan.operations[0].operation_id,) * 2},
            {"expires_at": "2000-01-01T00:00:00Z"}, {"mode": "LIVE"},
            {"approved_at": "2099-01-01T00:00:00Z"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(SafetyError):
                self.engine.run(plan, approval(plan, **changes))
        auth = approval(plan)
        self.engine.revoke(auth.approval_id)
        with self.assertRaisesRegex(SafetyError, "APPROVAL_REVOKED"):
            self.engine.run(plan, auth)
        self.assertEqual(self.adapter.calls, [])

    def test_proposal_revision_and_operation_id_cannot_be_reused(self):
        plan = self.rename_plan()
        self.engine.register(plan)
        changed = replace(plan, proposal_revision="revision-2")
        with self.assertRaisesRegex(SafetyError, "BATCH_ALREADY_FROZEN"):
            self.engine.register(changed)
        other = replace(plan, run_id="new", batch_id="new")
        with self.assertRaisesRegex(SafetyError, "OPERATION_ID_REUSED"):
            self.engine.register(other)

    def test_reconciliation_and_rollback_require_the_frozen_plan(self):
        plan = self.rename_plan()
        self.adapter.failure = "after_apply"
        auth = approval(plan)
        self.engine.run(plan, auth)
        changed = replace(plan, proposal_revision="changed-without-approval")
        with self.assertRaises(SafetyError):
            self.engine.reconcile(changed, changed.operations[0])
        with self.assertRaises(SafetyError):
            self.engine.rollback_plan(changed, [changed.operations[0].operation_id], "undo")
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.engine.latest(plan.operations[0].operation_id)["kind"], "UNCERTAIN")

    def test_explicit_terminal_evidence_resolves_pending_request_without_redispatch(self):
        for applies in (False, True):
            with self.subTest(applies=applies), tempfile.TemporaryDirectory() as temp:
                adapter = MockAdapter(copy.deepcopy(self.adapter.states))
                engine = Engine(Journal(Path(temp) / "journal"), adapter)
                plan = self.rename_plan()
                auth = approval(plan)
                adapter.failure = "pending"
                engine.run(plan, auth)
                key = engine.latest(plan.operations[0].operation_id)["payload"]["attempt_key"]
                adapter.settle_pending(key, apply=applies)
                calls = len(adapter.calls)
                result = engine.run(plan, auth)
                expected = "RECONCILED_AT_TARGET" if applies else "FAILED_NOT_APPLIED"
                self.assertEqual(set(result.values()), {expected})
                self.assertEqual(len(adapter.calls), calls)

    def test_live_adapter_is_hard_disabled_before_reads(self):
        self.adapter.is_mock = False
        plan = self.rename_plan()
        with self.assertRaisesRegex(SafetyError, "LIVE_EXECUTION_DISABLED"):
            self.engine.run(plan, approval(plan))
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.adapter.read_calls, [])

    def test_withdrawals_survive_new_proposals_and_never_read_source(self):
        self.engine.exclude("doc")
        plan = self.rename_plan()
        for action in (lambda: self.engine.dry_run(plan), lambda: self.engine.run(plan, approval(plan))):
            with self.assertRaisesRegex(SafetyError, "EXCLUDED"):
                action()
        forbidden_state = fixture_state(WITHDRAWN_ID, "Do not read.pdf")
        forbidden = self.rename_plan("forbidden", WITHDRAWN_ID, before=forbidden_state)
        with self.assertRaisesRegex(SafetyError, "EXCLUDED"):
            self.engine.dry_run(forbidden)
        self.assertEqual(self.adapter.read_calls, [])
        self.assertEqual(self.adapter.calls, [])

    def test_excluded_folder_descendant_blocks_without_source_reads(self):
        folder = fixture_state("folder", "Club", FOLDER)
        folder["subtree"] = {"complete": True, "members": [{"id": WITHDRAWN_ID}]}
        op = Operation("folder-move", "folder", "MOVE", folder, {"parents": ["destination"]})
        plan = Plan("batch", "batch", "r1", (op,))
        with self.assertRaises(SafetyError):
            self.engine.dry_run(plan)
        self.assertEqual(self.adapter.read_calls, [])

    def test_native_display_suffixes_and_uploaded_extension_changes_rejected(self):
        for mime, old, new in (
            ("application/vnd.google-apps.document", "Minutes", "Minutes.gdoc"),
            ("application/vnd.google-apps.spreadsheet", "Budget", "Budget.gsheet"),
            ("application/vnd.google-apps.presentation", "Intro", "Intro.gslides"),
            ("application/pdf", "Budget.pdf", "Budget"),
            ("application/gzip", "Archive.tar.gz", "Archive.zip"),
        ):
            with self.subTest(mime=mime):
                state = fixture_state("doc", old, mime)
                op = Operation("rename", "doc", "RENAME", state, {"name": new})
                with self.assertRaises(SafetyError):
                    op.validate()
        Operation("native", "doc", "RENAME", fixture_state("doc", "Minutes", "application/vnd.google-apps.document"), {"name": "2026 Minutes"}).validate()
        Operation("uploaded", "doc", "RENAME", self.original, {"name": "2026 Budget.pdf"}).validate()

    def test_transitive_same_file_dependency_refreshes_known_version(self):
        first = Operation("one", "doc", "RENAME", self.original, {"name": "First.pdf"})
        middle_state = fixture_state("other", "Other.pdf")
        self.adapter.states["other"] = middle_state
        middle = Operation("two", "other", "RENAME", middle_state, {"name": "Other renamed.pdf"}, ("one",))
        last = Operation("three", "doc", "RENAME", first.intended(), {"name": "Last.pdf"}, ("two",))
        plan = Plan("batch", "batch", "r1", (first, middle, last))
        self.assertEqual(set(self.engine.run(plan, approval(plan)).values()), {"VERIFIED_APPLIED"})
        inverse = self.engine.rollback_plan(plan, [op.operation_id for op in plan.operations], "undo")
        self.assertEqual(set(self.engine.run(inverse, approval(inverse)).values()), {"VERIFIED_APPLIED"})
        self.assertEqual(self.adapter.states["doc"]["name"], "Budget.pdf")

    def test_active_transitive_dependent_prevents_partial_rollback(self):
        first = Operation("one", "doc", "RENAME", self.original, {"name": "First.pdf"})
        other = fixture_state("other", "Other.pdf")
        self.adapter.states["other"] = other
        skipped = Operation("two", "other", "RENAME", other, {"name": "Other.pdf"}, ("one",))
        third = Operation("three", "other", "RENAME", other, {"name": "Changed.pdf"}, ("two",))
        plan = Plan("batch", "batch", "r1", (first, skipped, third))
        result = self.engine.run(plan, approval(plan))
        self.assertEqual(result["two"], "SKIPPED")
        with self.assertRaisesRegex(SafetyError, "ROLLBACK_HAS_ACTIVE_DEPENDENTS"):
            self.engine.rollback_plan(plan, ["one"], "undo")


if __name__ == "__main__":
    unittest.main()
