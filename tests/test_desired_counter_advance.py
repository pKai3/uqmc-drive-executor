"""Fresh version-only reconciliation: fake Drive, temporary journals, no network."""
import copy
import unittest

import test_desired as fixtures
from uqmc_recovery.desired import core


class CounterAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DesiredStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.files["fixture-source"].update(
            owners=[{"emailAddress": "fixture@example.invalid"}],
            sha1Checksum="fixture-sha1", sha256Checksum="fixture-sha256",
            capabilities={"canRename": True, "canMoveItemWithinDrive": True,
                          "canMoveItemOutOfDrive": True, "canAddChildren": False})
        self.fixture.freeze()
        self.journal = self.fixture.journal
        self.transport = self.fixture.transport
        self.runner = self.fixture.runner(audit_permissions=False)
        self.item = self.runner.items[0]

    def run_plan(self, runner=None, **kwargs):
        return self.fixture.run_plan(runner or self.runner, **kwargs)

    def comparisons(self):
        return [r["payload"] for r in self.journal.records()
                if r["kind"] == "DESIRED_STATE_COMPARISON"]

    def at_prewrite(self, changes):
        next_read = self.transport.reads.get("fixture-source", 0) + 2
        def mutate(source_id, count):
            if source_id == "fixture-source" and count == next_read:
                self.transport.files[source_id].update(copy.deepcopy(changes))
        self.transport.on_get = mutate

    def test_predispatch_counter_advance_binds_fresh_evidence_and_rolls_back(self):
        old_plan_hash = self.runner.plan_sha256
        self.at_prewrite({"version": "2"})
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 1})
        comparison = self.comparisons()[-1]
        self.assertEqual(comparison["outcome"], "ACCEPTED_VERSION_ONLY")
        self.assertEqual(comparison["stage"], "PRE_DISPATCH")
        self.assertEqual(comparison["differences"], {"version": {"before": "1", "observed": "2"}})
        self.assertEqual(self.journal.read_evidence(comparison["before_ref"])["version"], "1")
        self.assertEqual(self.journal.read_evidence(comparison["observed_ref"])["version"], "2")
        prepared = next(r for r in self.journal.records() if r["kind"] == "DESIRED_PREPARED")
        attempted = next(r for r in self.journal.records() if r["kind"] == "DESIRED_ATTEMPTED")
        self.assertEqual(self.journal.read_evidence(prepared["payload"]["before_ref"])["version"], "1")
        self.assertEqual(self.journal.read_evidence(attempted["payload"]["before_ref"])["version"], "2")
        self.assertLess(prepared["seq"], attempted["seq"])
        self.assertEqual(old_plan_hash, self.runner.plan_sha256)
        self.assertEqual(self.transport.permission_reads, {})
        self.assertEqual(self.run_plan(rollback=True)["counts"], {"ROLLED_BACK": 1})
        self.assertEqual(core(self.transport.files["fixture-source"]), self.item["original"])
        self.assertEqual(len(self.transport.calls), 2)

    def test_substantive_fresh_changes_block_and_retain_actual_comparison(self):
        original = copy.deepcopy(self.transport.files["fixture-source"])
        changes = {"name": "Human name.pdf", "parents": ["fixture-destination"],
            "mimeType": "image/png", "size": "123456789", "md5Checksum": "changed-md5",
            "sha1Checksum": "changed-sha1", "sha256Checksum": "changed-sha256", "owners": [],
            "capabilities": {"canRename": False}, "modifiedTime": "2026-02-01T00:00:00Z",
            "_http_etag": '"different-tag"'}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.transport.files["fixture-source"] = copy.deepcopy(original)
                self.at_prewrite({"version": "2", field: value})
                self.assertEqual(self.run_plan()["counts"], {"CONFLICT": 1})
                comparison = self.comparisons()[-1]
                self.assertEqual(comparison["outcome"], "CONFLICT")
                saved = self.journal.read_evidence(comparison["observed_ref"])
                self.assertEqual(saved[field], value)
                self.assertEqual(saved["version"], "2")
                self.assertEqual(self.journal.read_evidence(comparison["before_ref"]), original)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(any(r["kind"] == "DESIRED_ATTEMPTED" for r in self.journal.records()))

    def test_nonadvancing_missing_or_non_numeric_version_is_not_accepted(self):
        for version in ("0", "unknown", None):
            with self.subTest(version=version):
                self.transport.files["fixture-source"]["version"] = "1"
                self.at_prewrite({"version": version})
                self.assertEqual(self.run_plan()["counts"], {"CONFLICT": 1})
                self.assertEqual(self.comparisons()[-1]["outcome"], "CONFLICT")
        self.assertEqual(self.transport.calls, [])

    def test_known_capability_disappearance_is_not_counter_only(self):
        self.at_prewrite({"version": "2", "capabilities": None})
        self.assertEqual(self.run_plan()["counts"], {"CONFLICT": 1})
        self.assertIn("capabilities", self.comparisons()[-1]["differences"])
        self.assertEqual(self.transport.calls, [])

    def test_permission_drift_still_blocks_when_auditing_enabled(self):
        self.at_prewrite({"version": "2", "permissions": []})
        result = self.run_plan(self.fixture.runner(audit_permissions=True))
        self.assertEqual(result["counts"], {"CONFLICT": 1})
        self.assertEqual(result["items"][0]["reason"], "PERMISSION_DRIFT")
        self.assertIn("_permissions", self.comparisons()[-1]["differences"])
        self.assertEqual(self.transport.calls, [])

    def test_completed_counter_advance_stays_complete_and_preview_is_read_only(self):
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 1})
        active = self.runner.latest_change(self.item)
        old = self.journal.read_evidence(active["payload"]["applied_ref"])
        self.transport.files["fixture-source"]["version"] = str(int(old["version"]) + 1)
        previous = self.journal.path.read_bytes()
        restarted = self.fixture.runner(audit_permissions=False)
        self.assertEqual(restarted.preview()["counts"], {"TARGET": 1})
        self.assertEqual(self.journal.path.read_bytes(), previous)
        self.assertEqual(self.run_plan(restarted)["counts"], {"TARGET": 1})
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.comparisons()[-1]["stage"], "RECORDED_APPLIED")
        self.assertEqual(self.comparisons()[-1]["outcome"], "ACCEPTED_VERSION_ONLY")
        self.assertEqual(self.journal.read_evidence(active["payload"]["applied_ref"]), old)

    def test_completed_substantive_drift_is_not_reopened(self):
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 1})
        original = copy.deepcopy(self.transport.files["fixture-source"])
        for field, value in {"sha256Checksum": "changed", "capabilities": {},
                "modifiedTime": "2026-02-01T00:00:00Z"}.items():
            with self.subTest(field=field):
                self.transport.files["fixture-source"] = copy.deepcopy(original)
                self.transport.files["fixture-source"].update(version="3", **{field: value})
                self.assertEqual(self.run_plan()["counts"], {"CONFLICT": 1})
                self.assertEqual(self.comparisons()[-1]["outcome"], "CONFLICT")
        self.assertEqual(len(self.transport.calls), 1)

    def test_partial_recorded_rename_with_counter_advance_only_moves(self):
        with self.journal.locked():
            raw, _, _ = self.runner.observe(self.item)
            self.assertEqual(self.runner.transition(self.item, raw,
                core(raw) | {"name": self.item["target"]["name"]})["state"], "APPLIED")
        self.transport.files["fixture-source"]["version"] = "3"
        restarted = self.fixture.runner(audit_permissions=False)
        self.assertEqual(self.run_plan(restarted)["counts"], {"APPLIED": 1})
        self.assertEqual(self.transport.calls[-1], {"fileId": "fixture-source",
            "addParents": "fixture-destination", "removeParents": "fixture-parent"})
        self.assertEqual(len(self.transport.calls), 2)

    def test_uncertain_attempt_counter_advance_does_not_enable_retry(self):
        def die(stage, item):
            if stage == "before_dispatch":
                raise fixtures.ProcessDeath()
        with self.assertRaises(fixtures.ProcessDeath):
            self.run_plan(self.fixture.runner(audit_permissions=False, fault=die))
        self.assertIsNotNone(self.runner.pending(self.item, "FORWARD"))
        self.transport.files["fixture-source"]["version"] = "2"
        self.assertEqual(self.run_plan(self.fixture.runner(audit_permissions=False))["counts"], {"CONFLICT": 1})
        self.assertEqual(self.comparisons()[-1]["stage"], "PENDING_REQUEST")
        self.assertEqual(self.comparisons()[-1]["outcome"], "CONFLICT")
        self.assertEqual(self.transport.calls, [])
        self.assertIsNotNone(self.runner.pending(self.item, "FORWARD"))

    def test_retry_keeps_uncertain_request_guard_in_same_invocation(self):
        # The first write times out before our fake applies it. The caller
        # cannot use that knowledge: it must reconcile, then keep the guard.
        def timeout(args):
            raise TimeoutError("synthetic request outcome unknown")
        self.transport.before_update = timeout
        def counter_changes(source_id, count):
            # initial read, prewrite, readback, retry read, next prewrite
            if source_id == "fixture-source" and count == 5:
                self.transport.files[source_id]["version"] = "2"
        self.transport.on_get = counter_changes
        self.assertEqual(self.run_plan()["counts"], {"CONFLICT": 1})
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.comparisons()[-1]["stage"], "PRE_DISPATCH")
        self.assertEqual(self.comparisons()[-1]["outcome"], "CONFLICT")
        self.assertIsNotNone(self.runner.pending(self.item, "FORWARD"))

    def test_prepared_only_interruption_can_restart_without_replaying_effect(self):
        def die(source_id, count):
            if source_id == "fixture-source" and count == 2:
                raise fixtures.ProcessDeath()
        self.transport.on_get = die
        with self.assertRaises(fixtures.ProcessDeath):
            self.run_plan()
        self.assertTrue(any(r["kind"] == "DESIRED_PREPARED" for r in self.journal.records()))
        self.assertIsNone(self.runner.pending(self.item, "FORWARD"))
        self.transport.files["fixture-source"]["version"] = "2"
        self.transport.on_get = None
        self.assertEqual(self.run_plan(self.fixture.runner(audit_permissions=False))["counts"], {"APPLIED": 1})
        self.assertEqual(len(self.transport.calls), 1)

    def test_remote_success_then_death_reconciles_target_without_second_write(self):
        def die(stage, item):
            if stage == "after_dispatch":
                raise fixtures.ProcessDeath()
        with self.assertRaises(fixtures.ProcessDeath):
            self.run_plan(self.fixture.runner(audit_permissions=False, fault=die))
        self.transport.files["fixture-source"]["version"] = "3"
        self.assertEqual(self.run_plan(self.fixture.runner(audit_permissions=False))["counts"], {"APPLIED": 1})
        self.assertEqual(len(self.transport.calls), 1)
        active = self.runner.latest_change(self.item)
        self.assertEqual(active["payload"]["request_outcome"], "UNKNOWN_RECOVERED_BY_READ")
        self.assertIsNone(self.runner.pending(self.item, "FORWARD"))


if __name__ == "__main__":
    unittest.main()
