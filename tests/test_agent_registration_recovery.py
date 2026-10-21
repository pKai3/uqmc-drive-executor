"""Registration respects verified recovery across the legacy/direct boundary."""
import copy
import tempfile
import unittest
from pathlib import Path

from uqmc_recovery.agent_profile import register_agent_plan
from uqmc_recovery.destination_history import build_history_index
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import Operation, Plan, SafetyError


def observed(source="file", name="Original.pdf", parent="old"):
    return {"id": source, "name": name, "mime_type": "application/pdf",
            "parents": [parent], "parents_complete": True}


class LegacyRegistrationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.j = Journal(Path(self.temp.name) / "state")
        self.original = observed()
        self.renamed = observed(name="Approved.pdf")
        self.moved = observed(name="Approved.pdf", parent="destination")
        self.plan = Plan("old-batch", "old-batch", "old-revision", (
            Operation("old-rename", "file", "RENAME", self.original, {"name": "Approved.pdf"}),
            Operation("old-move", "file", "MOVE", self.renamed, {"parents": ["destination"]}, ("old-rename",)),
        ))
        register_agent_plan(self.j, self.plan)
        self.candidate = Plan("new-batch", "new-batch", "new-revision", (
            Operation("new-move", "another-file", "MOVE", observed("another-file"), {"parents": ["destination"]}),
        ))

    def legacy(self, kind="UNCERTAIN", **extra):
        return self.j.append(kind, "old-rename", payload={
            "file_id": "file", "proposal_hash": self.plan.proposal_hash,
            "direction": "FORWARD", "before_ref": self.j.evidence(self.original),
            "intended_ref": self.j.evidence(self.renamed), **extra})

    def desired(self, before, after, *, kind="DESIRED_APPLIED", source="file", plan=None):
        plan = plan or self.plan
        return self.j.append(kind, "desired-" + source, payload={
            "plan_sha256": plan.proposal_hash, "source_id": source, "direction": "FORWARD",
            "before_ref": self.j.evidence(before), "target_ref": self.j.evidence(after),
            "observed_ref": self.j.evidence(after), "applied_ref": self.j.evidence(after),
            "historical_uncertain_operation_ids": ["old-rename"]})

    def assert_fenced(self):
        with self.assertRaisesRegex(SafetyError, "UNRESOLVED_PRIOR_REQUEST"):
            register_agent_plan(self.j, self.candidate)

    def test_legacy_uncertain_then_verified_rename_and_move_allows_shared_parent_plan(self):
        uncertain = self.legacy()
        self.desired(self.original, self.renamed)
        self.desired(self.renamed, self.moved)
        before = self.j.records()
        result = register_agent_plan(self.j, self.candidate)
        self.assertEqual(result["kind"], "DRAFTED")
        self.assertEqual(self.j.records()[:-1], before)
        h = build_history_index(self.j)["file"]
        self.assertEqual(h["pending_requests"], [])
        self.assertIn("old-rename", h["historical_uncertain_operation_ids"])
        self.assertIn(uncertain["event_id"], h["event_ids"])

    def test_legacy_attempted_then_verified_rename_is_reconciled(self):
        self.legacy("ATTEMPTED")
        self.desired(self.original, self.renamed)
        self.assertEqual(register_agent_plan(self.j, self.candidate)["kind"], "DRAFTED")

    def test_unreconciled_legacy_request_remains_fenced(self):
        self.legacy()
        self.assert_fenced()

    def test_nonterminal_observation_cannot_release_fence(self):
        self.legacy()
        self.desired(self.original, self.renamed, kind="DESIRED_OBSERVED")
        self.assert_fenced()

    def test_later_verified_move_without_requested_rename_keeps_fence(self):
        self.legacy()
        # This receipt proves only a move: the pending old rename is unsatisfied.
        moved_original = observed(parent="destination")
        self.desired(self.original, moved_original)
        self.assert_fenced()

    def test_other_source_success_cannot_release_fence(self):
        self.legacy()
        self.desired(observed("other"), observed("other", name="Approved.pdf"), source="other")
        self.assert_fenced()

    def test_wrong_identity_in_same_source_evidence_fails_closed(self):
        self.legacy()
        wrong = copy.deepcopy(self.renamed)
        wrong["id"] = "other"
        self.desired(self.original, wrong)
        with self.assertRaisesRegex(SafetyError, "DESTINATION_HISTORY_ID_MISMATCH"):
            register_agent_plan(self.j, self.candidate)

    def test_prepared_handoff_stays_fenced_despite_later_success(self):
        self.legacy("PREPARED", bridge="AGENT_ASSISTED")
        self.desired(self.original, self.renamed)
        self.assert_fenced()

    def test_success_before_legacy_request_cannot_release_fence(self):
        self.desired(self.original, self.renamed)
        self.legacy()
        self.assert_fenced()

    def test_new_unresolved_desired_attempt_keeps_fence(self):
        self.legacy()
        self.desired(self.original, self.renamed)
        self.desired(self.renamed, self.moved, kind="DESIRED_ATTEMPTED")
        self.assert_fenced()


if __name__ == "__main__":
    unittest.main()
