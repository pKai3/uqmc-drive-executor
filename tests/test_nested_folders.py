"""Nested desired-state proofs using synthetic Drive data and temporary journals."""
import copy
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

import test_desired as fixtures
import test_desired_parallel as parallel_fixtures
from test_desired import FakeDrive, ProcessDeath
from uqmc_recovery.desired import core
from uqmc_recovery.model import FOLDER


class NestedDrive(FakeDrive):
    """Model provider counters advancing on the ancestors of a changed child."""

    def __init__(self, files, journal):
        super().__init__(files)
        self.journal = journal

    def ancestors(self, source_id):
        pending = list(self.files[source_id]["parents"])
        seen = set()
        while pending:
            parent_id = pending.pop()
            if parent_id in seen:
                continue
            seen.add(parent_id)
            pending.extend(self.files[parent_id]["parents"])
        return seen

    def update_metadata(self, args, *, etag=None):
        intents = [r for r in self.journal.records()
                   if r["kind"] == "DESIRED_ATTEMPTED"
                   and r["payload"]["source_id"] == args["fileId"]]
        if not intents or intents[-1]["payload"]["args"] != args:
            raise AssertionError("Missing durable matching intent before dispatch")
        old_ancestors = self.ancestors(args["fileId"])
        value = super().update_metadata(args, etag=etag)
        for parent_id in old_ancestors | self.ancestors(args["fileId"]):
            parent = self.files[parent_id]
            parent["version"] = str(int(parent["version"]) + 1)
            parent["_http_etag"] = '"fixture-v' + parent["version"] + '"'
        return value


class NestedFolderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DesiredStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.direct_snapshot = self.fixture.snapshot
        self.fixture.snapshot = self.snapshot

    def snapshot(self, source_id):
        value = self.direct_snapshot(source_id)
        if value["mime_type"] == FOLDER:
            pending, seen, members = [source_id], set(), []
            while pending:
                parent_id = pending.pop()
                if parent_id in seen:
                    raise AssertionError("Cycle in synthetic fixture")
                seen.add(parent_id)
                for item in self.fixture.files.values():
                    if parent_id in item["parents"]:
                        members.append({"id": item["id"], "name": item["name"],
                            "mime_type": item["mimeType"], "parents": list(item["parents"])})
                        if item["mimeType"] == FOLDER:
                            pending.append(item["id"])
            value["subtree"] = {"complete": True, "membership_scope": "DESCENDANTS",
                                "members": members}
        return value

    def folder(self, source_id, name, parent="fixture-parent"):
        self.fixture.add_file(source_id, name, parent)
        value = self.fixture.files[source_id]
        value["mimeType"] = FOLDER
        value.pop("size", None)
        value.pop("md5Checksum", None)

    def freeze(self, operations):
        # The historical plan requires explicit ordering for overlapping folder
        # scopes. Desired compilation replaces these legacy operation edges.
        ordered = []
        for operation in operations:
            dependencies = list(operation.depends_on)
            if ordered and ordered[-1].operation_id not in dependencies:
                dependencies.append(ordered[-1].operation_id)
            ordered.append(replace(operation, depends_on=tuple(dependencies)))
        # The established fixture includes every fake file in access baselines;
        # this graph also has untouched descendants, outside that exact scope.
        required = {operation.file_id for operation in ordered}
        for operation in ordered:
            required.update(operation.before["parents"])
            required.update(operation.target.get("parents", []))
        evidence = self.fixture.journal.evidence
        def scoped_evidence(value):
            if value.get("kind") == "SCOPED_EXPOSURE_REVIEW":
                value = value | {"access_baselines": {key: ref
                    for key, ref in value["access_baselines"].items() if key in required}}
            return evidence(value)
        with patch.object(self.fixture.journal, "evidence", side_effect=scoped_evidence):
            self.fixture.freeze(ordered)
        self.fixture.transport = NestedDrive(self.fixture.transport.files, self.fixture.journal)
        self.transport = self.fixture.transport
        self.journal = self.fixture.journal
        return self.fixture.runner(audit_permissions=False)

    def runner(self, **kwargs):
        return self.fixture.runner(audit_permissions=False, **kwargs)

    def run_plan(self, runner=None, **kwargs):
        return self.fixture.run_plan(runner or self.runner(), **kwargs)

    def outgoing(self):
        self.folder("fixture-outer", "Old outer")
        self.folder("fixture-inner", "Old inner", "fixture-outer")
        self.fixture.files["fixture-source"]["parents"] = ["fixture-inner"]
        self.fixture.add_file("fixture-sibling", "Sibling.pdf", "fixture-outer")
        self.fixture.add_file("fixture-stable", "Stable.pdf", "fixture-inner")
        # Deliberately list descendants first: inferred containment orders them.
        return self.freeze((*self.fixture.operations(),
            *self.fixture.operations("fixture-sibling", target_name="Sibling approved.pdf"),
            *self.fixture.operations("fixture-inner", target_name="Inner"),
            *self.fixture.operations("fixture-outer", target_name="Outer")))

    def incoming(self):
        self.folder("fixture-outer", "Old receiver")
        self.folder("fixture-inner", "Old incoming")
        self.fixture.files["fixture-source"]["parents"] = ["fixture-inner"]
        self.fixture.add_file("fixture-stable", "Stable.pdf", "fixture-inner")
        return self.freeze((*self.fixture.operations("fixture-outer", target_name="Receiver"),
            *self.fixture.operations("fixture-inner", target_name="Incoming", target_parent="fixture-outer"),
            *self.fixture.operations(target_parent="fixture-inner")))

    def calls(self):
        return [call["fileId"] for call in self.transport.calls]

    def assert_originals(self, runner):
        for item in runner.items:
            self.assertEqual(core(self.transport.files[item["source_id"]]), item["original"])

    def test_nested_sources_and_siblings_wait_for_original_containers(self):
        runner = self.outgoing()
        self.assertIn("fixture-inner", runner.by_id["fixture-source"]["dependencies"])
        self.assertIn("fixture-outer", runner.by_id["fixture-inner"]["dependencies"])
        self.assertIn("fixture-outer", runner.by_id["fixture-sibling"]["dependencies"])
        blocked = self.run_plan(runner, source_ids=["fixture-source"])
        self.assertEqual(blocked["counts"], {"DEPENDENCY_BLOCKED": 1})
        self.assertEqual(self.calls(), [])
        result = self.run_plan()
        self.assertEqual(result["counts"], {"APPLIED": 4})
        calls = self.calls()
        self.assertLess(calls.index("fixture-outer"), calls.index("fixture-inner"))
        self.assertLess(calls.index("fixture-inner"), calls.index("fixture-source"))
        self.assertLess(calls.index("fixture-outer"), calls.index("fixture-sibling"))

    def test_resume_accepts_own_child_counters_and_preserves_applied_evidence(self):
        self.outgoing()
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 4})
        applied = [r for r in self.journal.records() if r["kind"] == "DESIRED_APPLIED"]
        original_evidence = {r["payload"]["applied_ref"]:
            self.journal.read_evidence(r["payload"]["applied_ref"]) for r in applied}
        outer = next(r for r in applied if r["payload"]["source_id"] == "fixture-outer")
        old = original_evidence[outer["payload"]["applied_ref"]]
        self.assertGreater(int(self.transport.files["fixture-outer"]["version"]), int(old["version"]))
        self.assertEqual(self.run_plan()["counts"], {"TARGET": 4})
        self.assertEqual(len(self.calls()), 4)
        for ref, value in original_evidence.items():
            self.assertEqual(self.journal.read_evidence(ref), value)
        self.assertTrue(any(r["kind"] == "DESIRED_FOLDER_RECONCILED" for r in self.journal.records()))

    def test_parallel_nested_siblings_follow_parent_without_duplicate_dispatch(self):
        self.outgoing()
        server = parallel_fixtures.SharedDrive(self.transport.files)
        server.drive = NestedDrive(self.transport.files, self.journal)
        server.journal = self.journal
        self.transport = server.drive
        self.fixture.transport = server.client()
        barrier = threading.Barrier(2, timeout=5)
        def overlap(args, ordinal):
            if args["fileId"] in {"fixture-inner", "fixture-sibling"}:
                barrier.wait()
        server.before = overlap
        result = self.run_plan(workers=3, transport_factory=server.client)
        self.assertEqual(result["counts"], {"APPLIED": 4})
        calls = self.calls()
        self.assertEqual(len(calls), len(set(calls)))
        self.assertLess(calls.index("fixture-outer"), calls.index("fixture-inner"))
        self.assertLess(calls.index("fixture-outer"), calls.index("fixture-sibling"))
        self.assertLess(calls.index("fixture-inner"), calls.index("fixture-source"))
        self.assertGreaterEqual(server.peak, 2)
        self.assertEqual(server.duplicates, [])
        self.assertEqual(server.permission_calls, 0)

    def test_preview_of_own_child_counter_changes_is_read_only_until_resume(self):
        self.outgoing()
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 4})
        before = self.journal.path.read_bytes()
        self.assertEqual(self.runner().preview()["counts"], {"TARGET": 4})
        self.assertEqual(self.journal.path.read_bytes(), before)
        self.assertFalse(any(r["kind"] == "DESIRED_FOLDER_RECONCILED" for r in self.journal.records()))
        self.assertEqual(self.run_plan()["counts"], {"TARGET": 4})
        self.assertTrue(any(r["kind"] == "DESIRED_FOLDER_RECONCILED" for r in self.journal.records()))
        self.assertEqual(len(self.calls()), 4)

    def test_unexplained_folder_counter_advance_without_child_action_is_rejected(self):
        self.outgoing()
        self.assertEqual(self.run_plan(limit=1)["counts"], {"APPLIED": 1})
        self.assertEqual(self.calls(), ["fixture-outer"])
        raw = self.transport.files["fixture-outer"]
        raw["version"] = str(int(raw["version"]) + 1)
        raw["_http_etag"] = '"external-unexplained-counter"'
        result = self.run_plan(source_ids=["fixture-outer"])
        self.assertEqual(result["counts"], {"CONFLICT": 1})
        self.assertEqual(self.calls(), ["fixture-outer"])

    def test_nested_rollback_can_restart_after_first_two_inverses(self):
        runner = self.outgoing()
        self.assertEqual(self.run_plan(runner)["counts"], {"APPLIED": 4})
        self.assertEqual(self.run_plan(rollback=True, limit=2)["counts"], {"ROLLED_BACK": 2})
        self.assertEqual(self.run_plan(rollback=True)["counts"], {"ROLLED_BACK": 2})
        rollback_calls = self.calls()[4:]
        self.assertLess(rollback_calls.index("fixture-source"), rollback_calls.index("fixture-inner"))
        self.assertLess(rollback_calls.index("fixture-inner"), rollback_calls.index("fixture-outer"))
        self.assertLess(rollback_calls.index("fixture-sibling"), rollback_calls.index("fixture-outer"))
        self.assert_originals(runner)
        self.run_plan(rollback=True)
        self.assertEqual(len(self.calls()), 8)

    def test_forward_restart_reconciles_child_success_before_parent_counter(self):
        self.outgoing()
        def die(point, item):
            if point == "after_dispatch" and item["source_id"] == "fixture-source":
                raise ProcessDeath()
        with self.assertRaises(ProcessDeath):
            self.run_plan(self.runner(fault=die))
        result = self.run_plan()
        self.assertTrue(all(item["state"] in {"TARGET", "APPLIED"} for item in result["items"]), result)
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual(self.calls().count("fixture-source"), 1)
        self.assertEqual(self.run_plan()["counts"], {"TARGET": 4})

    def test_rollback_restart_reconciles_child_success_once(self):
        runner = self.outgoing()
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 4})
        def die(point, item):
            if point == "after_dispatch" and item["source_id"] == "fixture-source":
                raise ProcessDeath()
        with self.assertRaises(ProcessDeath):
            self.run_plan(self.runner(fault=die), rollback=True)
        result = self.run_plan(rollback=True)
        self.assertTrue(all(item["state"] == "ROLLED_BACK" for item in result["items"]), result)
        self.assert_originals(runner)
        self.assertEqual(len(self.calls()), 8)
        self.assertEqual(self.calls().count("fixture-source"), 2)

    def test_incoming_folder_with_recursive_descendants_survives_resume_and_rollback(self):
        runner = self.incoming()
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 3})
        self.assertEqual(self.calls(), ["fixture-outer", "fixture-inner", "fixture-source"])
        self.assertEqual(self.run_plan()["counts"], {"TARGET": 3})
        self.assertEqual(self.run_plan(rollback=True)["counts"], {"ROLLED_BACK": 3})
        self.assertEqual(self.calls()[3:], ["fixture-source", "fixture-inner", "fixture-outer"])
        self.assert_originals(runner)
        self.assertEqual(self.transport.files["fixture-stable"]["parents"], ["fixture-inner"])

    def test_moving_ancestor_into_former_child_uses_target_order_without_cycle(self):
        self.folder("fixture-outer", "Old outer")
        self.folder("fixture-inner", "Old inner", "fixture-outer")
        self.fixture.files["fixture-source"]["parents"] = ["fixture-inner"]
        runner = self.freeze((*self.fixture.operations("fixture-outer", target_name="Outer", target_parent="fixture-inner"),
            *self.fixture.operations("fixture-inner", target_name="Inner")))
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 2})
        self.assertEqual(self.calls(), ["fixture-inner", "fixture-outer"])
        self.assertEqual(self.run_plan(rollback=True)["counts"], {"ROLLED_BACK": 2})
        self.assertEqual(self.calls()[2:], ["fixture-outer", "fixture-inner"])
        self.assert_originals(runner)

    def test_unplanned_descendant_name_drift_blocks_container_move(self):
        self.outgoing()
        self.transport.files["fixture-stable"]["name"] = "External rename.pdf"
        result = self.run_plan()
        self.assertEqual(fixtures.DesiredStateTests.states(result)["fixture-outer"], "CONFLICT")
        self.assertEqual(self.calls(), [])

    def test_descendant_reparenting_inside_same_tree_blocks_container_move(self):
        self.outgoing()
        self.transport.files["fixture-stable"]["parents"] = ["fixture-outer"]
        result = self.run_plan()
        self.assertEqual(fixtures.DesiredStateTests.states(result)["fixture-outer"], "CONFLICT")
        self.assertEqual(self.calls(), [])

    def test_external_added_member_blocks_reconciliation_after_own_child_changes(self):
        self.incoming()
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 3})
        intruder = copy.deepcopy(self.transport.files["fixture-stable"])
        intruder.update(id="fixture-intruder", name="External.pdf", parents=["fixture-outer"])
        self.transport.files[intruder["id"]] = intruder
        result = self.run_plan()
        self.assertEqual(fixtures.DesiredStateTests.states(result)["fixture-outer"], "CONFLICT")
        self.assertEqual(len(self.calls()), 3)

    def test_external_missing_member_blocks_reconciliation_after_own_child_changes(self):
        self.incoming()
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 3})
        self.transport.files["fixture-stable"]["parents"] = ["fixture-destination"]
        result = self.run_plan()
        states = fixtures.DesiredStateTests.states(result)
        self.assertEqual(states["fixture-outer"], "CONFLICT")
        self.assertEqual(len(self.calls()), 3)

    def test_external_folder_name_drift_remains_conflict_after_own_child_changes(self):
        self.outgoing()
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 4})
        self.transport.files["fixture-outer"]["name"] = "External folder name"
        result = self.run_plan()
        self.assertEqual(fixtures.DesiredStateTests.states(result)["fixture-outer"], "CONFLICT")
        self.assertEqual(len(self.calls()), 4)

    def test_external_folder_parent_drift_blocks_rollback_after_own_child_changes(self):
        self.outgoing()
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 4})
        self.transport.files["fixture-outer"]["parents"] = []
        result = self.run_plan(rollback=True)
        self.assertNotIn("fixture-outer", self.calls()[4:])
        self.assertIn(fixtures.DesiredStateTests.states(result)["fixture-outer"], {"CONFLICT", "DEPENDENCY_BLOCKED"})

    def test_rollback_final_source_read_is_strict_after_own_child_counter_reconciliation(self):
        self.outgoing()
        self.assertEqual(self.run_plan()["counts"], {"APPLIED": 4})
        changed = []
        def concurrent_counter(source_id, count):
            if source_id != "fixture-outer" or changed:
                return
            prepared = [r for r in self.journal.records() if r["kind"] == "DESIRED_PREPARED"
                and r["payload"]["source_id"] == source_id and r["payload"]["direction"] == "ROLLBACK"]
            if prepared:
                changed.append(source_id)
                raw = self.transport.files[source_id]
                raw["version"] = str(int(raw["version"]) + 1)
                raw["_http_etag"] = '"external-after-preflight"'
        self.transport.on_get = concurrent_counter
        result = self.run_plan(rollback=True)
        self.assertEqual(changed, ["fixture-outer"])
        self.assertEqual(fixtures.DesiredStateTests.states(result)["fixture-outer"], "CONFLICT")
        self.assertNotIn("fixture-outer", self.calls()[4:])
        self.assertEqual(len(self.calls()), 7)


if __name__ == "__main__":
    unittest.main()
