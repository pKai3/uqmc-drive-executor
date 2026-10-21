"""Local rollback candidates and registration guards; no connector or dispatch."""
import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from uqmc_recovery.agent_profile import (adjust_descendant_renames, agent_snapshot,
    register_agent_plan, rollback_agent_plan)
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, Operation, Plan, SafetyError
from test_agent_profile import observed


class AgentRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / "state")

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, operations):
        return Plan("forward", "forward", "revision", tuple(operations))

    def record(self, plan, op, *, after=None, kind="VERIFIED_APPLIED"):
        before = agent_snapshot(op.before)
        intended = agent_snapshot(op.intended())
        return self.journal.append(kind, operation_id=op.operation_id, attempt=1, payload={
            "run_id": plan.run_id, "proposal_hash": plan.proposal_hash,
            "file_id": op.file_id, "action": op.action, "direction": op.direction,
            "original_operation_id": op.original_operation_id, "depends_on": list(op.depends_on),
            "before_ref": self.journal.evidence(before),
            "intended_ref": self.journal.evidence(intended),
            "observed_ref": self.journal.evidence(agent_snapshot(after or intended)),
        })

    def execute_fixture(self, plan):
        register_agent_plan(self.journal, plan)
        return [self.record(plan, op) for op in plan.ordered()]

    def compound(self):
        move = Operation("move", "file", "MOVE", observed(), {"parents": ["target"]})
        rename = Operation("rename", "file", "RENAME", move.intended(), {"name": "Renamed"}, ("move",))
        return self.plan((move, rename))

    def test_candidate_is_reverse_linked_and_never_approves(self):
        plan = self.compound()
        self.execute_fixture(plan)
        before_events = self.journal.records()
        inverse = rollback_agent_plan(self.journal, plan, ["move", "rename"], "undo")
        self.assertEqual(self.journal.records(), before_events)
        self.assertEqual([op.original_operation_id for op in inverse.operations], ["rename", "move"])
        self.assertEqual(inverse.operations[0].target, {"name": "Notes"})
        self.assertEqual(inverse.operations[1].target, {"parents": ["old"]})
        self.assertEqual(inverse.operations[1].depends_on, (inverse.operations[0].operation_id,))
        register_agent_plan(self.journal, inverse)
        self.assertFalse(any(r["kind"] in {"APPROVED", "PREPARED", "ATTEMPTED"} for r in self.journal.records()))

    def test_unfrozen_or_changed_plan_rejected(self):
        plan = self.compound()
        with self.assertRaisesRegex(SafetyError, "FROZEN_AGENT_PLAN_MISMATCH"):
            rollback_agent_plan(self.journal, plan, ["move"], "undo")
        self.execute_fixture(plan)
        with self.assertRaisesRegex(SafetyError, "FROZEN_AGENT_PLAN_MISMATCH"):
            rollback_agent_plan(self.journal, replace(plan, proposal_revision="changed"), ["move"], "undo")

    def test_uncertain_unrelated_request_also_blocks(self):
        plan = self.compound()
        self.execute_fixture(plan)
        self.journal.append("UNCERTAIN", "unrelated", payload={"file_id": "other"})
        with self.assertRaisesRegex(SafetyError, "RECONCILE_BEFORE_ROLLBACK"):
            rollback_agent_plan(self.journal, plan, ["move", "rename"], "undo")

    def test_reserved_handoff_blocks(self):
        plan = self.compound()
        self.execute_fixture(plan)
        self.journal.append("PREPARED", "unrelated", payload={"bridge": "agent", "file_id": "other"})
        with self.assertRaisesRegex(SafetyError, "RECONCILE_BEFORE_ROLLBACK"):
            rollback_agent_plan(self.journal, plan, ["move", "rename"], "undo")

    def test_active_dependency_outside_selection_blocks(self):
        plan = self.compound()
        self.execute_fixture(plan)
        with self.assertRaisesRegex(SafetyError, "ROLLBACK_HAS_ACTIVE_DEPENDENTS"):
            rollback_agent_plan(self.journal, plan, ["move"], "undo")

    def test_only_verified_partial_success_is_selected(self):
        plan = self.compound()
        register_agent_plan(self.journal, plan)
        self.record(plan, plan.operations[0], kind="RECONCILED_AT_TARGET")
        inverse = rollback_agent_plan(self.journal, plan, ["move", "rename"], "undo")
        self.assertEqual([op.original_operation_id for op in inverse.operations], ["move"])

    def test_postimage_drift_or_event_wrong_hash_rejected(self):
        plan = self.compound()
        register_agent_plan(self.journal, plan)
        bad = plan.operations[0].intended()
        bad["name"] = "Manual"
        self.record(plan, plan.operations[0], after=bad)
        with self.assertRaisesRegex(SafetyError, "ROLLBACK_EXACT_POSTIMAGE_REQUIRED"):
            rollback_agent_plan(self.journal, plan, ["move"], "undo")

    def test_observed_move_inheritance_is_preserved_in_inverse(self):
        plan = self.plan((self.compound().operations[0],))
        register_agent_plan(self.journal, plan)
        after = plan.operations[0].intended()
        after["permissions"] = [{"id": "inherited", "role": "reader"}]
        after["access"] = {"complete": False}
        self.record(plan, plan.operations[0], after=after)
        inverse = rollback_agent_plan(self.journal, plan, ["move"], "undo")
        self.assertEqual(inverse.operations[0].before["permissions"], after["permissions"])
        register_agent_plan(self.journal, inverse)

    def test_handcrafted_inverse_cannot_accept_manual_state(self):
        plan = self.compound()
        self.execute_fixture(plan)
        inverse = rollback_agent_plan(self.journal, plan, ["rename"], "undo")
        changed = copy.deepcopy(inverse.operations[0].before)
        changed["name"] = "Manual title"
        bad = replace(inverse, operations=(replace(inverse.operations[0], before=changed),))
        with self.assertRaisesRegex(SafetyError, "ROLLBACK_POSTIMAGE_BASELINE_MISMATCH"):
            register_agent_plan(self.journal, bad)

    def test_handcrafted_inverse_cannot_change_restoration_target(self):
        plan = self.compound()
        self.execute_fixture(plan)
        inverse = rollback_agent_plan(self.journal, plan, ["rename"], "undo")
        bad = replace(inverse, operations=(replace(inverse.operations[0], target={"name": "Arbitrary"}),))
        with self.assertRaisesRegex(SafetyError, "ROLLBACK_NOT_EXACT_PREIMAGE"):
            register_agent_plan(self.journal, bad)

    def test_already_reverted_is_skipped_and_distinct_second_inverse_rejected(self):
        plan = self.compound()
        self.execute_fixture(plan)
        inverse = rollback_agent_plan(self.journal, plan, ["rename"], "undo")
        register_agent_plan(self.journal, inverse)
        self.record(inverse, inverse.operations[0])
        register_agent_plan(self.journal, inverse)  # Identical completed draft is idempotent.
        self.assertEqual(rollback_agent_plan(self.journal, plan, ["rename"], "again").operations, ())
        other = replace(inverse, run_id="again", batch_id="again", operations=(replace(inverse.operations[0], operation_id="another-inverse"),))
        with self.assertRaisesRegex(SafetyError, "ROLLBACK_ALREADY_REVERTED"):
            register_agent_plan(self.journal, other)

    def test_historical_wrong_extension_restores_only_exact_original(self):
        before = observed(name="Certificate.CPR", mime="application/pdf")
        provenance = self.journal.evidence({"fixture": "readback"})
        proof = self.journal.evidence({"kind": "VERIFIED_EXTENSION_CORRECTION", "source_id": "file",
            "source_exact_name": "Certificate.CPR", "target_exact_name": "Certificate.pdf",
            "mime_type": "application/pdf", "provenance_ref": provenance})
        rename = Operation("rename", "file", "RENAME", before, {"name": "Certificate.pdf"},
            extension_correction={"source_suffix": ".CPR", "target_suffix": ".pdf", "mime_type": "application/pdf", "evidence_ref": proof})
        plan = self.plan((rename,))
        self.execute_fixture(plan)
        inverse = rollback_agent_plan(self.journal, plan, ["rename"], "undo")
        self.assertEqual(inverse.operations[0].target, {"name": "Certificate.CPR"})
        register_agent_plan(self.journal, inverse)

    def test_independent_actions_reverse_actual_success_order(self):
        first = Operation("a", "file-a", "RENAME", observed("file-a"), {"name": "A"})
        second = Operation("b", "file-b", "RENAME", observed("file-b"), {"name": "B"})
        plan = self.plan((first, second))
        register_agent_plan(self.journal, plan)
        self.record(plan, second)
        self.record(plan, first)
        result = rollback_agent_plan(self.journal, plan, ["a", "b"], "undo")
        self.assertEqual([op.original_operation_id for op in result.operations], ["a", "b"])

    def test_child_rename_evidence_changes_only_embedded_known_name(self):
        folder = observed("folder", "Folder", FOLDER)
        folder["subtree"]["members"] = [{"id": "child", "name": "Old"}, {"id": "other"}]
        child = Operation("child-rename", "child", "RENAME", observed("child", "Old", parents=["folder"]), {"name": "New"})
        event = self.record(self.plan((child,)), child)
        result = adjust_descendant_renames(folder, [event], self.journal)
        self.assertEqual(result["subtree"]["members"], [{"id": "child", "name": "New"}, {"id": "other"}])
        self.assertEqual(folder["subtree"]["members"][0]["name"], "Old")
        folder["subtree"]["members"][0]["name"] = "Manual"
        with self.assertRaisesRegex(SafetyError, "DESCENDANT_RENAME_BASELINE_MISMATCH"):
            adjust_descendant_renames(folder, [event], self.journal)

    def test_root_rename_child_rename_root_move_inverse_chain(self):
        folder = observed("folder", "Old folder", FOLDER)
        folder["subtree"]["members"] = [{"id": "child", "name": "Old child"}]
        root_rename = Operation("root-name", "folder", "RENAME", folder, {"name": "New folder"})
        child_rename = Operation("child-name", "child", "RENAME", observed("child", "Old child", parents=["folder"]),
                                 {"name": "New child"}, ("root-name",))
        root_before_move = root_rename.intended()
        root_before_move["subtree"]["members"][0]["name"] = "New child"
        root_move = Operation("root-move", "folder", "MOVE", root_before_move, {"parents": ["destination"]}, ("child-name",))
        plan = self.plan((root_rename, child_rename, root_move))
        self.execute_fixture(plan)
        inverse = rollback_agent_plan(self.journal, plan, ["root-name", "child-name", "root-move"], "undo")
        self.assertEqual([op.original_operation_id for op in inverse.operations], ["root-move", "child-name", "root-name"])
        self.assertEqual(inverse.operations[-1].before["subtree"]["members"][0]["name"], "Old child")
        register_agent_plan(self.journal, inverse)

    def test_multiple_known_child_renames_are_idempotent_at_final_name(self):
        folder = observed("folder", "Folder", FOLDER)
        folder["subtree"]["members"] = [{"id": "child", "name": "Old"}]
        first = Operation("first", "child", "RENAME", observed("child", "Old", parents=["folder"]), {"name": "Middle"})
        second = Operation("second", "child", "RENAME", first.intended(), {"name": "Final"}, ("first",))
        plan = self.plan((first, second))
        events = [self.record(plan, first), self.record(plan, second)]
        result = adjust_descendant_renames(folder, events, self.journal)
        self.assertEqual(result["subtree"]["members"][0]["name"], "Final")
        self.assertEqual(adjust_descendant_renames(result, events, self.journal), result)


if __name__ == "__main__":
    unittest.main()
