"""Approved identical names preserve all identities, including after restart."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from uqmc_recovery.adapters import MockAdapter, fixture_state
from uqmc_recovery.engine import Engine
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, Operation, Plan, SafetyError
from test_engine import approval, ProcessDeath


class CollisionPolicyTests(unittest.TestCase):
    def batch(self, pairs=1):
        states = {key: fixture_state(key, key, FOLDER, ())
                  for key in ("origin-a", "origin-b", "target")}
        operations = []
        for index in range(pairs):
            ids = (f"fixture-{index}-a", f"fixture-{index}-b")
            for offset, source_id in enumerate(ids):
                states[source_id] = fixture_state(source_id, f"Certificate {index}.pdf",
                                                 parents=(f"origin-{'ab'[offset]}",))
                operations.append(Operation(f"move-{source_id}", source_id, "MOVE", states[source_id],
                                            {"parents": ["target"]},
                                            allowed_collision_ids=(ids[1 - offset],)))
        return states, Plan("collision-batch", "collision-batch", "reviewed-collision-scope", tuple(operations))

    def test_all_32_approved_pairs_preserve_64_distinct_ids_and_names(self):
        states, plan = self.batch(32)
        with tempfile.TemporaryDirectory() as tmp:
            adapter = MockAdapter(states)
            engine = Engine(Journal(Path(tmp) / "state"), adapter)
            self.assertEqual(set(engine.run(plan, approval(plan)).values()), {"VERIFIED_APPLIED"})
            for op in plan.operations:
                self.assertEqual(adapter.states[op.file_id]["parents"], ["target"])
                self.assertEqual(adapter.states[op.file_id]["name"], states[op.file_id]["name"])
            self.assertEqual(len(adapter.calls), 64)
            self.assertEqual(len(adapter.states), 67)

    def test_partial_pair_restart_does_not_repeat_first_or_reject_second(self):
        states, plan = self.batch()
        with tempfile.TemporaryDirectory() as tmp:
            adapter = MockAdapter(states)
            path = Path(tmp) / "state"
            auth = approval(plan)
            def crash(point):
                if point == "after_VERIFIED_APPLIED":
                    raise ProcessDeath(point)
            with self.assertRaises(ProcessDeath):
                Engine(Journal(path), adapter, fault=crash).run(plan, auth)
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(set(Engine(Journal(path), adapter).run(plan, auth).values()),
                             {"VERIFIED_APPLIED"})
            self.assertEqual(len(adapter.calls), 2)

    def test_unexpected_third_identity_still_blocks_both_operations(self):
        states, plan = self.batch()
        states["unexpected"] = fixture_state("unexpected", "Certificate 0.pdf", parents=("target",))
        with tempfile.TemporaryDirectory() as tmp:
            adapter = MockAdapter(states)
            result = Engine(Journal(Path(tmp) / "state"), adapter).run(plan, approval(plan))
            self.assertEqual(set(result.values()), {"BLOCKED_CONFLICT"})
            self.assertEqual(adapter.calls, [])

    def test_collision_scope_changes_invalidate_frozen_approval(self):
        _, plan = self.batch()
        changed = replace(plan, operations=(replace(plan.operations[0], allowed_collision_ids=("different",)),
                                            plan.operations[1]))
        self.assertNotEqual(changed.proposal_hash, plan.proposal_hash)

    def test_unknown_same_name_is_blocked_without_explicit_id_scope(self):
        states, plan = self.batch()
        plan = replace(plan, operations=tuple(replace(op, allowed_collision_ids=()) for op in plan.operations))
        with tempfile.TemporaryDirectory() as tmp:
            adapter = MockAdapter(states)
            result = Engine(Journal(Path(tmp) / "state"), adapter).run(plan, approval(plan))
            self.assertEqual(list(result.values()), ["VERIFIED_APPLIED", "BLOCKED_CONFLICT"])
            self.assertEqual(len(adapter.calls), 1)

    def test_excluded_and_self_peer_ids_rejected(self):
        _, plan = self.batch()
        for peers, excludes in (((plan.operations[0].file_id,), ()), (("blocked-peer",), ("blocked-peer",))):
            modified = replace(plan, operations=(replace(plan.operations[0], allowed_collision_ids=peers),),
                               excluded_ids=excludes)
            with self.assertRaises(SafetyError):
                modified.validate()

    def test_reverse_moves_restore_original_separate_parents(self):
        states, plan = self.batch()
        with tempfile.TemporaryDirectory() as tmp:
            adapter = MockAdapter(states)
            engine = Engine(Journal(Path(tmp) / "state"), adapter)
            engine.run(plan, approval(plan))
            undo = engine.rollback_plan(plan, [op.operation_id for op in plan.operations], "undo-collision-batch")
            self.assertEqual(set(engine.run(undo, approval(undo)).values()), {"VERIFIED_APPLIED"})
            for op in plan.operations:
                self.assertEqual(adapter.states[op.file_id]["parents"], op.before["parents"])
                self.assertEqual(adapter.states[op.file_id]["name"], op.before["name"])


if __name__ == "__main__":
    unittest.main()
