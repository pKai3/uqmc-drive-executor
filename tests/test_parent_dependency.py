"""Synthetic receiver checks: volatile versions differ from actual parent drift."""
import copy
import threading
import unittest

import test_desired as fixtures
import test_desired_parallel as parallel
from uqmc_recovery.desired import DesiredRunner
from uqmc_recovery.model import FOLDER, SafetyError


class ParentDependencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DesiredStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.server = None

    def receiver(self, child_count=1):
        self.fixture.add_file("fixture-receiver", "Old receiver")
        folder = self.fixture.files["fixture-receiver"]
        folder["mimeType"] = FOLDER
        folder.pop("size", None)
        folder.pop("md5Checksum", None)
        folder["capabilities"] = {"canAddChildren": True}
        folder["owners"] = [{"emailAddress": "owner@example.invalid"}]
        self.child_ids = ["fixture-source"]
        for index in range(1, child_count):
            identity = "fixture-child-" + str(index)
            self.fixture.add_file(identity, "Original " + str(index) + ".pdf")
            self.child_ids.append(identity)
        operations = list(self.fixture.operations("fixture-receiver", target_name="Receiver"))
        for index, identity in enumerate(self.child_ids):
            operations.extend(self.fixture.operations(identity,
                target_name="Approved " + str(index) + ".pdf", target_parent="fixture-receiver"))
        self.fixture.freeze(operations)
        return self.prepare()

    def prepare(self):
        if self.server is None:
            self.server = parallel.SharedDrive(self.fixture.transport.files)
            self.server.journal = self.fixture.journal
        return DesiredRunner(self.fixture.journal, self.fixture.plan, self.server.client(),
            audit_permissions=False, retry_delay=0)

    def run_selected(self, runner, source_ids, workers=3):
        return runner.run(apply=True, confirm_plan_sha256=self.fixture.plan.proposal_hash,
            source_ids=source_ids, workers=workers, transport_factory=self.server.client)

    def apply_receiver(self, child_count=1):
        runner = self.receiver(child_count)
        result = self.run_selected(runner, ["fixture-receiver"], workers=1)
        self.assertEqual(result["counts"], {"APPLIED": 1})
        self.assertEqual([a["fileId"] for a in self.server.attempts], ["fixture-receiver"])
        return runner

    def bump_receiver_version(self):
        with self.server.lock:
            folder = self.server.drive.files["fixture-receiver"]
            folder["version"] = str(int(folder["version"]) + 1)
            folder["_http_etag"] = '"receiver-v' + folder["version"] + '"'
            folder["modifiedTime"] = "2026-01-02T00:00:00Z"

    def test_applied_receiver_version_advance_allows_exact_child_subset_without_receiver_replay(self):
        self.apply_receiver(2)
        self.bump_receiver_version()
        result = self.run_selected(self.prepare(), [self.child_ids[0]])
        self.assertEqual(result["counts"], {"APPLIED": 1})
        self.assertEqual([a["fileId"] for a in self.server.attempts],
            ["fixture-receiver", self.child_ids[0]])
        self.assertEqual(self.server.drive.files[self.child_ids[1]]["parents"], ["fixture-parent"])
        self.assertEqual(self.server.permission_calls, 0)

    def test_substantive_receiver_drift_blocks_external_dependency_before_any_child_write(self):
        self.apply_receiver()
        original = copy.deepcopy(self.server.drive.files["fixture-receiver"])
        changes = {
            "name": {"name": "Human changed receiver"},
            "location": {"parents": ["fixture-parent"]},
            "trash": {"trashed": True},
            "capability_denied": {"capabilities": {"canAddChildren": False}},
            "capability_missing": {"capabilities": {}},
            "mime": {"mimeType": "application/pdf"},
            "owner": {"owners": [{"emailAddress": "different@example.invalid"}]},
        }
        for label, changed in changes.items():
            with self.subTest(change=label):
                self.server.drive.files["fixture-receiver"] = copy.deepcopy(original)
                self.server.drive.files["fixture-receiver"].update(changed)
                result = self.run_selected(self.prepare(), self.child_ids, workers=1)
                self.assertEqual(result["counts"], {"DEPENDENCY_BLOCKED": 1})
                self.assertEqual(len(self.server.attempts), 1)
        self.assertEqual(self.server.permission_calls, 0)

    def test_same_receiver_is_strict_when_it_is_the_actual_source(self):
        self.apply_receiver()
        self.bump_receiver_version()
        runner = self.prepare()
        item = runner.by_id["fixture-receiver"]
        self.assertEqual(runner.observe(item, dependency=True)[2], "TARGET")
        with self.assertRaisesRegex(SafetyError, "CONCURRENT_CHANGE"):
            runner.observe(item)
        self.assertEqual(len(self.server.attempts), 1)

    def test_completed_certificate_source_version_drift_stays_strict(self):
        self.apply_receiver()
        self.assertEqual(self.run_selected(self.prepare(), self.child_ids)["counts"], {"APPLIED": 1})
        source = self.server.drive.files[self.child_ids[0]]
        source["version"] = str(int(source["version"]) + 1)
        result = self.run_selected(self.prepare(), self.child_ids)
        self.assertEqual(result["counts"], {"CONFLICT": 1})
        self.assertEqual(result["items"][0]["reason"], "CONCURRENT_CHANGE")
        self.assertEqual(len(self.server.attempts), 2)

    def test_parallel_sibling_updates_can_advance_receiver_version(self):
        self.apply_receiver(6)
        barrier = threading.Barrier(3, timeout=5)
        def overlap_first_wave(args, ordinal):
            if ordinal <= 4:  # The folder was attempt one, then three child workers.
                barrier.wait()
        self.server.before = overlap_first_wave
        self.server.after = lambda args, value: self.bump_receiver_version()
        result = self.run_selected(self.prepare(), self.child_ids, workers=3)
        self.assertEqual(result["counts"], {"APPLIED": 6})
        self.assertEqual(self.server.peak, 3)
        self.assertEqual([a["fileId"] for a in self.server.attempts].count("fixture-receiver"), 1)
        self.assertEqual(set(a["fileId"] for a in self.server.attempts[1:]), set(self.child_ids))
        self.assertEqual(self.server.duplicates, [])
        self.assertEqual(self.server.permission_calls, 0)

    def assert_later_sibling_blocked(self, changed, expected_reason):
        self.apply_receiver(2)
        def change_parent_after_first_child(args, value):
            if args["fileId"] == self.child_ids[0]:
                with self.server.lock:
                    self.server.drive.files["fixture-receiver"].update(changed)
        self.server.after = change_parent_after_first_child
        result = self.run_selected(self.prepare(), self.child_ids, workers=1)
        states = {item["source_id"]: item for item in result["items"]}
        self.assertEqual(states[self.child_ids[0]]["state"], "APPLIED")
        self.assertEqual(states[self.child_ids[1]]["state"], "CONFLICT")
        self.assertEqual(states[self.child_ids[1]]["reason"], expected_reason)
        self.assertEqual([a["fileId"] for a in self.server.attempts],
            ["fixture-receiver", self.child_ids[0]])
        self.assertEqual(self.server.permission_calls, 0)

    def test_parent_name_drift_after_earlier_sibling_blocks_later_child(self):
        self.assert_later_sibling_blocked({"name": "External edit"}, "UNAPPROVED_NAME_DRIFT")

    def test_parent_capability_drift_after_earlier_sibling_blocks_later_child(self):
        self.assert_later_sibling_blocked({"capabilities": {"canAddChildren": False}},
            "DEPENDENCY_CAPABILITY_DRIFT")

    def test_parent_location_must_be_satisfied_even_if_original_location_is_approved(self):
        runner = self.receiver()
        child = runner.by_id[self.child_ids[0]]
        raw, _, _ = runner.observe(child)
        with self.assertRaisesRegex(SafetyError, "PARENT_DEPENDENCY_LOCATION_CONFLICT"):
            runner.preflight(child, raw, child["target"])
        self.assertEqual(self.server.attempts, [])


if __name__ == "__main__":
    unittest.main()
