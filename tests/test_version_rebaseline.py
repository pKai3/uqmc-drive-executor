"""Explicit historical version reconciliation; synthetic Drive and temp WAL only."""
import copy
import unittest

import test_desired as fixture_module
from uqmc_recovery.desired import core
from uqmc_recovery.model import SafetyError


class VersionRebaselineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DesiredStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.files["fixture-source"].update(
            owners=[{"emailAddress": "fixture@example.invalid"}],
            capabilities={"canRename": True, "canMoveItemWithinDrive": True,
                          "canMoveItemOutOfDrive": True, "canAddChildren": False})
        self.fixture.freeze()
        self.runner = self.fixture.runner(audit_permissions=False)
        self.journal = self.fixture.journal
        self.transport = self.fixture.transport
        self.item = self.runner.items[0]
        with self.journal.locked():
            raw, _, _ = self.runner.observe(self.item)
            result = self.runner.transition(self.item, raw,
                core(raw) | {"name": self.item["target"]["name"]})
        self.assertEqual(result["state"], "APPLIED")
        self.applied = self.runner.latest_change(self.item)
        self.applied_raw = self.journal.read_evidence(self.applied["payload"]["applied_ref"])
        self.transport.calls.clear()
        self.transport.files["fixture-source"]["version"] = str(int(self.applied_raw["version"]) + 1)
        self.authority = {"schema_version": "uqmc.version-rebaseline.authorization.v1",
            "plan_sha256": self.runner.plan_sha256, "source_ids": ["fixture-source"],
            "instruction": "Synthetic explicit approval to reconcile this exact historical version drift."}
        self.authority_ref = self.journal.evidence(self.authority)

    def reconcile(self, runner=None, authority_ref=None):
        runner = runner or self.runner
        return runner.rebaseline_version_only(runner.by_id["fixture-source"],
            authorization_ref=authority_ref or self.authority_ref)

    def test_explicit_rebaseline_preserves_history_and_restart_moves_only(self):
        previous = self.journal.path.read_bytes()
        self.reconcile()
        self.assertTrue(self.journal.path.read_bytes().startswith(previous))
        self.assertEqual(self.journal.read_evidence(self.applied["payload"]["applied_ref"]), self.applied_raw)
        event = self.journal.records()[-1]
        self.assertEqual(event["kind"], "DESIRED_REBASELINED")
        self.assertEqual(event["payload"]["historical_applied_ref"], self.applied["payload"]["applied_ref"])
        self.assertEqual(self.journal.read_evidence(event["payload"]["observed_ref"])["version"],
                         self.transport.files["fixture-source"]["version"])
        self.assertEqual(self.transport.calls, [])
        restarted = self.fixture.runner(audit_permissions=False)
        self.assertEqual(restarted.observe(restarted.items[0])[2], "PARTIAL")
        result = self.fixture.run_plan(restarted)
        self.assertEqual(result["counts"], {"APPLIED": 1})
        self.assertEqual(self.transport.calls, [{"fileId": "fixture-source",
            "addParents": "fixture-destination", "removeParents": "fixture-parent"}])
        self.assertEqual(self.transport.permission_reads, {})
        self.assertEqual(self.fixture.run_plan(self.fixture.runner(audit_permissions=False))["counts"], {"TARGET": 1})
        self.assertEqual(len(self.transport.calls), 1)

    def test_substantive_or_missing_known_fields_cannot_be_rebaselined(self):
        original = copy.deepcopy(self.transport.files["fixture-source"])
        changes = {"name": "Human name.pdf", "parents": ["fixture-other-parent"],
            "mimeType": "image/png", "size": "101", "md5Checksum": "different",
            "owners": [], "modifiedTime": "2026-01-02T00:00:00Z", "_http_etag": '"new-etag"',
            "trashed": True, "capabilities": {"canRename": True, "canMoveItemWithinDrive": False,
                "canMoveItemOutOfDrive": True, "canAddChildren": False}}
        previous = self.journal.path.read_bytes()
        for field, value in changes.items():
            with self.subTest(field=field):
                self.transport.files["fixture-source"] = copy.deepcopy(original)
                self.transport.files["fixture-source"][field] = value
                with self.assertRaises(SafetyError):
                    self.reconcile()
                self.assertEqual(self.journal.path.read_bytes(), previous)
        self.transport.files["fixture-source"] = copy.deepcopy(original)
        del self.transport.files["fixture-source"]["md5Checksum"]
        with self.assertRaises(SafetyError):
            self.reconcile()
        self.assertEqual(self.transport.calls, [])

    def test_nonadvancing_or_unavailable_version_rejected(self):
        for value in (self.applied_raw["version"], "0", "unknown", None):
            with self.subTest(version=value):
                self.transport.files["fixture-source"]["version"] = value
                with self.assertRaises(SafetyError):
                    self.reconcile()
        self.assertFalse(any(r["kind"] == "DESIRED_REBASELINED" for r in self.journal.records()))

    def test_later_version_change_remains_a_conflict(self):
        self.reconcile()
        self.transport.files["fixture-source"]["version"] = str(int(self.transport.files["fixture-source"]["version"]) + 1)
        result = self.fixture.run_plan(self.fixture.runner(audit_permissions=False))
        self.assertEqual(result["counts"], {"CONFLICT": 1})
        self.assertEqual(result["items"][0]["reason"], "CONCURRENT_CHANGE")
        self.assertEqual(self.transport.calls, [])

    def test_change_between_observation_and_prewrite_still_blocks(self):
        self.reconcile()
        read_number = self.transport.reads["fixture-source"]
        def changed(source_id, count):
            if source_id == "fixture-source" and count == read_number + 2:
                self.transport.files[source_id]["version"] = str(int(self.transport.files[source_id]["version"]) + 1)
        self.transport.on_get = changed
        result = self.fixture.run_plan(self.fixture.runner(audit_permissions=False))
        self.assertEqual(result["counts"], {"CONFLICT": 1})
        self.assertEqual(self.transport.calls, [])

    def test_pending_attempt_cannot_be_rebaselined(self):
        self.runner.record("ATTEMPTED", self.item, before_ref=self.journal.evidence(self.applied_raw),
            target_ref=self.journal.evidence(self.item["target"]), attempt_id="fixture-interrupted")
        with self.assertRaises(SafetyError):
            self.reconcile()
        self.assertFalse(any(r["kind"] == "DESIRED_REBASELINED" for r in self.journal.records()))
        self.assertEqual(self.transport.calls, [])

    def test_authority_must_bind_exact_plan_and_source(self):
        for change in ({"plan_sha256": "0" * 64}, {"source_ids": ["fixture-other"]},
                       {"instruction": ""}, {"schema_version": "not-authority"}):
            with self.subTest(change=change):
                ref = self.journal.evidence(self.authority | change)
                with self.assertRaises(SafetyError):
                    self.reconcile(authority_ref=ref)
        self.assertEqual(self.transport.calls, [])

    def test_old_permission_audit_omission_does_not_require_new_permission_reads(self):
        self.transport.files["fixture-source"].pop("permissions")
        self.reconcile()
        result = self.fixture.run_plan(self.fixture.runner(audit_permissions=False))
        self.assertEqual(result["counts"], {"APPLIED": 1})
        self.assertEqual(self.transport.permission_reads, {})


if __name__ == "__main__":
    unittest.main()
