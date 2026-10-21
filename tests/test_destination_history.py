"""Synthetic history only; no real Drive calls or journal writes."""
import copy
import unittest

from uqmc_recovery.destination_history import build_history_index, reconcile_item
from uqmc_recovery.model import SafetyError, digest


class MemoryJournal:
    def __init__(self):
        self.events, self.values = [], {}
        self.read_count = 0

    def records(self):
        self.read_count += 1
        return copy.deepcopy(self.events)

    def evidence(self, value):
        ref = "sha256:" + digest(value)
        self.values[ref] = copy.deepcopy(value)
        return ref

    def read_evidence(self, ref):
        return copy.deepcopy(self.values[ref])

    def add(self, kind, payload, operation_id=None):
        event = {"seq": len(self.events)+1, "event_id": "event-"+str(len(self.events)+1),
                 "kind": kind, "payload": copy.deepcopy(payload), "operation_id": operation_id}
        self.events.append(event)
        return event


def state(name="Original.pdf", parents=None, **extra):
    return {"id": "fixture-file", "name": name, "parents": parents or ["fixture-source"],
            "mime_type": "application/pdf", **extra}


class DestinationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.j = MemoryJournal()
        self.original = state(size="100", md5_checksum="unchanged")
        self.renamed = state("Approved.pdf", size="100", md5_checksum="unchanged")
        self.moved = state(parents=["fixture-destination"], size="100", md5_checksum="unchanged")
        self.target = state("Approved.pdf", ["fixture-destination"], size="100", md5_checksum="unchanged")
        self.plan = {"run_id": "fixture-run", "batch_id": "fixture-run", "proposal_revision": "fresh-1",
            "excluded_ids": [], "operations": [
            {"operation_id": "rename", "file_id": "fixture-file", "action": "RENAME",
             "direction": "FORWARD", "before": self.original, "target": {"name": "Approved.pdf"}, "depends_on": []},
            {"operation_id": "move", "file_id": "fixture-file", "action": "MOVE", "direction": "FORWARD",
             "before": self.renamed, "target": {"parents": ["fixture-destination"]}, "depends_on": ["rename"]}]}
        self.hash = digest(self.plan)
        self.j.add("DRAFTED", {"proposal_hash": self.hash, "plan_ref": self.j.evidence(self.plan)})

    def legacy(self, action, observed, kind="VERIFIED_APPLIED"):
        op = self.plan["operations"][action == "move"]
        return self.j.add(kind, {"proposal_hash": self.hash, "file_id": "fixture-file", "direction": "FORWARD",
            "before_ref": self.j.evidence(op["before"]), "observed_ref": self.j.evidence(observed),
            "intended_ref": self.j.evidence({**op["before"], **op["target"]})}, action)

    def desired(self, kind, before=None, after=None, **extra):
        before, after = before or self.original, after or self.target
        return self.j.add(kind, {"plan_sha256": self.hash, "source_id": "fixture-file", "direction": "FORWARD",
            "before_ref": self.j.evidence(before), "target_ref": self.j.evidence(after),
            "applied_ref": self.j.evidence(after), "observed_ref": self.j.evidence(after), **extra}, "desired-fixture")

    def reconcile(self, live, desired=None, **kwargs):
        return reconcile_item(build_history_index(self.j)["fixture-file"], desired or self.target, live, **kwargs)

    def test_original_needs_both_fields(self):
        result = self.reconcile(self.original)
        self.assertEqual((result["disposition"], result["pending_fields"]), ("READY", ["name", "parents"]))
        self.assertFalse(result["previously_verified"])

    def test_legacy_rename_preserved_move_only(self):
        self.legacy("rename", self.renamed)
        result = self.reconcile(self.renamed)
        self.assertEqual(result["pending_fields"], ["parents"])
        self.assertEqual(result["completed_fields"], {"name": "Approved.pdf"})

    def test_legacy_move_preserved_rename_only(self):
        # A MOVE can have been independently approved before its rename.
        self.plan["operations"][1]["before"] = self.original
        self.hash = digest(self.plan)
        self.j.events[0]["payload"] = {"proposal_hash": self.hash, "plan_ref": self.j.evidence(self.plan)}
        self.legacy("move", self.moved)
        result = self.reconcile(self.moved)
        self.assertEqual(result["pending_fields"], ["name"])

    def test_combined_completed_skips_dispatch(self):
        self.desired("DESIRED_APPLIED")
        result = self.reconcile(self.target)
        self.assertEqual(result["disposition"], "COMPLETE")
        self.assertEqual(result["pending_fields"], [])
        h = build_history_index(self.j)["fixture-file"]
        self.assertEqual(h["last_applied"]["before"]["name"], self.original["name"])
        self.assertEqual(h["last_applied"]["after"]["parents"], self.target["parents"])

    def test_rename_only_desired_result_preserves_pending_move(self):
        self.desired("DESIRED_APPLIED", after=self.renamed)
        result = self.reconcile(self.renamed)
        self.assertEqual(result["pending_fields"], ["parents"])
        self.assertEqual(result["completed_fields"], {"name": "Approved.pdf"})

    def test_changed_completed_name_is_separate_work(self):
        self.legacy("rename", self.renamed)
        result = self.reconcile(self.renamed, {**self.target, "name": "Later.pdf"})
        self.assertEqual(result["reason"], "COMPLETED_EFFECT_CHANGED_REQUIRES_SEPARATE_WORK:name")

    def test_changed_completed_parent_is_separate_work(self):
        self.desired("DESIRED_APPLIED")
        result = self.reconcile(self.target, {**self.target, "parents": ["another-parent"]})
        self.assertEqual(result["reason"], "COMPLETED_EFFECT_CHANGED_REQUIRES_SEPARATE_WORK:parents")

    def test_changed_completed_logical_destination_is_separate_work(self):
        self.desired("DESIRED_APPLIED")
        result = self.reconcile(self.target, logical_destination="Section/New", prior_logical_destination="Section/Old")
        self.assertEqual(result["reason"], "COMPLETED_DESTINATION_CHANGED_REQUIRES_SEPARATE_WORK")

    def test_pending_proposed_name_can_change(self):
        result = self.reconcile(self.original, {**self.target, "name": "New pending.pdf"})
        self.assertEqual(result["disposition"], "READY")

    def test_external_name_drift_rejected(self):
        result = self.reconcile({**self.original, "name": "Human edit.pdf"})
        self.assertEqual(result["reason"], "UNAPPROVED_CURRENT_STATE:name")

    def test_completed_effect_manual_revert_rejected(self):
        self.legacy("rename", self.renamed)
        result = self.reconcile(self.original)
        self.assertEqual(result["reason"], "COMPLETED_EFFECT_DRIFT:name")

    def test_content_drift_rejected(self):
        result = self.reconcile({**self.original, "md5_checksum": "modified"})
        self.assertEqual(result["reason"], "PROTECTED_METADATA_DRIFT:md5Checksum")

    def test_optional_historically_unknown_size_can_be_enriched(self):
        h = build_history_index(self.j)["fixture-file"]
        h["baseline"].pop("size")
        result = reconcile_item(h, self.target, self.original)
        self.assertEqual(result["disposition"], "READY")

    def test_version_change_does_not_erase_substantive_checks(self):
        h = build_history_index(self.j)["fixture-file"]
        h["baseline"]["version"] = "1"
        self.assertEqual(reconcile_item(h, self.target, self.original | {"version": "2"})["disposition"], "READY")
        self.assertEqual(reconcile_item(h, self.target, self.original | {"version": "2", "size": "101"})["disposition"], "BLOCKED")

    def test_interrupted_before_dispatch_known_original_not_global_deadlock(self):
        self.desired("DESIRED_ATTEMPTED")
        result = self.reconcile(self.original)
        self.assertEqual(result["disposition"], "READY")
        self.assertEqual(result["current"]["name"], "Original.pdf")
        self.assertTrue(result["historical_request_uncertainty"])
        self.assertTrue(result["recovery_required"])

    def test_response_lost_after_success_requires_readback_record_not_dispatch(self):
        self.desired("DESIRED_ATTEMPTED")
        result = self.reconcile(self.target)
        self.assertEqual(result["disposition"], "RECOVER_AT_TARGET")
        self.assertEqual(result["pending_fields"], [])
        self.assertTrue(result["historical_request_uncertainty"])

    def test_unknown_request_target_change_requires_existing_plan_recovery(self):
        self.desired("DESIRED_ATTEMPTED")
        result = self.reconcile(self.original, self.target | {"name": "Different.pdf"})
        self.assertEqual(result["reason"], "PENDING_REQUEST_TARGET_CHANGED_RECONCILE_EXISTING_PLAN")

    def test_later_verified_result_clears_dispatch_pending_but_retains_unknown_history(self):
        self.legacy("rename", self.original, "UNCERTAIN")
        self.desired("DESIRED_APPLIED")
        result = self.reconcile(self.target)
        self.assertEqual(result["disposition"], "COMPLETE")
        self.assertTrue(result["historical_request_uncertainty"])
        self.assertFalse(result["recovery_required"])

    def test_successful_attempt_is_not_mislabeled_historically_uncertain(self):
        self.desired("DESIRED_ATTEMPTED")
        self.desired("DESIRED_APPLIED", request_outcome="RETURNED_SUCCESS")
        result = self.reconcile(self.target)
        self.assertEqual(result["disposition"], "COMPLETE")
        self.assertFalse(result["historical_request_uncertainty"])

    def test_lost_response_retains_uncertainty_after_verified_readback(self):
        self.desired("DESIRED_ATTEMPTED")
        self.desired("DESIRED_RECEIPT", request_outcome="UNKNOWN")
        self.desired("DESIRED_APPLIED", request_outcome="UNKNOWN_RECOVERED_BY_READ")
        result = self.reconcile(self.target)
        self.assertEqual(result["disposition"], "COMPLETE")
        self.assertFalse(result["recovery_required"])
        self.assertTrue(result["historical_request_uncertainty"])

    def test_rollback_requires_explicit_resume_not_automatic_reapply(self):
        forward = self.desired("DESIRED_APPLIED")
        self.desired("DESIRED_ROLLED_BACK", before=self.target, after=self.original,
                     direction="ROLLBACK", forward_event_id=forward["event_id"])
        result = self.reconcile(self.original)
        self.assertEqual(result["reason"], "ROLLED_BACK_EFFECT_REQUIRES_EXPLICIT_RESUME")
        self.assertEqual(result["completed_fields"], {})

    def test_fresh_authority_removal_or_exclusion_blocks_even_completed(self):
        self.desired("DESIRED_APPLIED")
        self.assertEqual(self.reconcile(self.target, approved=False)["reason"], "FRESH_SECTION_AUTHORITY_REMOVED")
        self.assertEqual(self.reconcile(self.target, excluded=True)["reason"], "SOURCE_EXCLUDED")

    def test_no_history_requires_exact_baseline_and_can_skip_current_desired(self):
        result = reconcile_item(None, self.target, self.original, baseline=self.original)
        self.assertEqual(result["disposition"], "READY")
        self.assertEqual(reconcile_item(None, self.target, self.target, baseline=self.original)["disposition"], "COMPLETE")
        result = reconcile_item(None, self.target, self.original | {"name": "unapproved"}, baseline=self.original)
        self.assertEqual(result["disposition"], "BLOCKED")

    def test_tampered_plan_rejected(self):
        ref = self.j.events[0]["payload"]["plan_ref"]
        self.j.values[ref]["proposal_revision"] = "tampered"
        with self.assertRaisesRegex(SafetyError, "PLAN_HASH_MISMATCH"):
            build_history_index(self.j)

    def test_unbound_verified_event_rejected(self):
        self.desired("DESIRED_APPLIED", plan_sha256="unbound")
        with self.assertRaisesRegex(SafetyError, "EVENT_NOT_IN_PLAN"):
            build_history_index(self.j)

    def test_evidence_wrong_identity_rejected(self):
        self.legacy("rename", self.renamed | {"id": "another-file"})
        with self.assertRaisesRegex(SafetyError, "ID_MISMATCH"):
            build_history_index(self.j)

    def test_no_writes_and_single_journal_snapshot(self):
        self.desired("DESIRED_APPLIED")
        before = copy.deepcopy((self.j.events, self.j.values))
        build_history_index(self.j)
        self.assertEqual((self.j.events, self.j.values), before)
        self.assertEqual(self.j.read_count, 1)


if __name__ == "__main__":
    unittest.main()
