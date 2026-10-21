import copy
import hashlib
import json
import unittest
from contextlib import nullcontext

from uqmc_recovery.projection import (
    HEADERS, WORKBOOK_STATES, build_projections, deliver_projection, delivery_key, source_head,
)


class MemoryJournal:
    """Projection test fixture; durability is covered by the journal tests."""
    def __init__(self):
        self.events = []
        self.evidence_values = {}

    def locked(self):
        return nullcontext()

    def records(self):
        return copy.deepcopy(self.events)

    def evidence(self, value):
        ref = "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        self.evidence_values[ref] = copy.deepcopy(value)
        return ref

    def read_evidence(self, ref):
        return copy.deepcopy(self.evidence_values[ref])

    def append(self, kind, operation_id=None, attempt=0, payload=None):
        seq = len(self.events) + 1
        event = {"schema_version": 1, "seq": seq, "event_id": f"event-{seq}",
                 "at": f"2026-10-21T10:00:{seq:02}Z", "kind": kind,
                 "operation_id": operation_id, "attempt": attempt,
                 "payload": copy.deepcopy(payload or {}), "prev_hash": "0" * 64,
                 "hash": hashlib.sha256(str(seq).encode()).hexdigest()}
        self.events.append(event)
        return event


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.journal = MemoryJournal()
        self.before = self.journal.evidence({
            "id": "source-private", "name": "=SECRET_FORMULA()", "mime_type": "text/plain",
            "parents": ["private-parent"], "permissions": [{"email": "private@example.test"}],
            "private_error": "token=do-not-export",
        })
        self.after = self.journal.evidence({
            "id": "source-private", "name": "Safe.txt", "mime_type": "text/plain",
            "parents": ["new-parent"], "permissions": [{"email": "private@example.test"}],
        })
        self.payload = {"run_id": "ignored-run", "batch_id": "batch-1", "proposal_revision": "r1",
                        "proposal_hash": "a" * 64, "action": "RENAME", "direction": "FORWARD",
                        "file_id": "source-private", "depends_on": [], "proposal_refs": ["RENAME-1"],
                        "actor": "PRIVATE_ACTOR", "before_ref": self.before,
                        "intended_ref": self.after, "observed_ref": self.after,
                        "reason_code": "NONE", "raw_error": "private@example.test"}

    def add(self, kind, op="rename-1", **overrides):
        payload = {**self.payload, **overrides}
        return self.journal.append(kind, op, 1, payload)

    def test_default_public_export_is_empty(self):
        self.add("VERIFIED_APPLIED")
        output = build_projections(self.journal)
        self.assertEqual(output["html"]["items"], [])
        self.assertEqual(output["html"]["operations"], [])
        self.assertEqual(len(output["workbook"]["rows"]), 1)

    def test_public_allowlist_redacts_nested_sensitive_data(self):
        self.add("VERIFIED_APPLIED", reason_code="private@example.test")
        self.add("VERIFIED_APPLIED", op="other-op", file_id="not-allowed")
        output = build_projections(self.journal, allowed_source_ids=["source-private"])
        public = output["html"]
        rendered = json.dumps(public)
        for secret in ("SECRET_FORMULA", "Safe.txt", "private-parent", "new-parent", "text/plain",
                       "private@example.test", "PRIVATE_ACTOR", "not-allowed", self.before, self.after,
                       "do-not-export"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(public["items"][0]["source_id"], "source-private")
        self.assertEqual(public["items"][0]["observed"]["exact_name"], None)
        self.assertEqual(public["operations"][0]["reason_code"], "REVIEW_REQUIRED")

    def test_same_snapshot_and_exact_existing_headers(self):
        self.add("PREPARED")
        output = build_projections(self.journal)
        self.assertEqual(output["html"]["snapshot_id"], output["workbook"]["snapshot_id"])
        self.assertEqual(output["html"]["schema_version"], "uqmc.execution.snapshot.v1")
        self.assertEqual(len(HEADERS), 26)
        self.assertEqual(output["workbook"]["headers"], HEADERS)
        self.assertTrue(all(len(r) == 26 for r in output["workbook"]["rows"]))
        self.assertEqual(output["workbook"]["rows"][0][2], "batch-1")

    def test_formula_name_remains_literal_for_raw_sheet_insert(self):
        self.add("PREPARED")
        private = build_projections(self.journal)["workbook"]
        self.assertEqual(private["valueInputOption"], "RAW")
        self.assertEqual(private["rows"][0][12], "=SECRET_FORMULA()")
        self.assertEqual(private["rows"][0][13], '["private-parent"]')

    def test_policy_events_do_not_become_log_rows(self):
        for kind in ("DRAFTED", "APPROVED", "REVOKED", "EXCLUDED"):
            self.add(kind)
        self.assertEqual(build_projections(self.journal)["workbook"]["rows"], [])

    def test_reconciled_mapping_preserves_attribution_uncertainty(self):
        self.add("RECONCILED_AT_TARGET")
        row = build_projections(self.journal)["workbook"]["rows"][0]
        self.assertIn(row[16], WORKBOOK_STATES)
        self.assertEqual(row[16], "VERIFIED_APPLIED")
        self.assertEqual(row[24], "OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN")

    def test_move_and_rename_remain_separate_and_failed_retry_preserves_success(self):
        self.add("VERIFIED_APPLIED", op="move-1", action="MOVE")
        self.add("VERIFIED_APPLIED")
        self.add("FAILED_NOT_APPLIED", op="rename-later")
        item = build_projections(self.journal)["workbook"]["items"][0]
        self.assertEqual(item["move"]["effect"], "APPLIED")
        self.assertEqual(item["rename"]["effect"], "APPLIED")
        self.assertEqual(item["rename"]["last_event_state"], "FAILED_NOT_APPLIED")
        self.assertEqual(item["location"]["effect"], "DIRECT")
        self.assertEqual(item["location"]["ancestor_move_operation_ids"], [])

    def test_rollback_links_and_changes_only_its_action_effect(self):
        self.add("VERIFIED_APPLIED", op="move-1", action="MOVE")
        self.add("VERIFIED_APPLIED")
        self.add("VERIFIED_APPLIED", op="undo-rename", direction="ROLLBACK",
                 original_operation_id="rename-1", observed_ref=self.before)
        private = build_projections(self.journal)["workbook"]
        item = private["items"][0]
        self.assertEqual(item["move"]["effect"], "APPLIED")
        self.assertEqual(item["rename"]["effect"], "REVERTED")
        self.assertEqual(private["operations"][-1]["related_forward_operation_id"], "rename-1")

    def test_no_log_does_not_claim_live_location_or_unchanged(self):
        public = build_projections(self.journal, allowed_source_ids=["untracked"])["html"]
        item = public["items"][0]
        self.assertEqual(item["move"]["effect"], "NOT_TRACKED")
        self.assertEqual(item["rename"]["effect"], "NOT_TRACKED")
        self.assertEqual(item["location"]["effect"], "UNKNOWN")
        self.assertEqual(item["observed"]["read_state"], "NEVER_VERIFIED")
        self.assertEqual(public["coverage"]["complete"], False)

    def test_unknown_and_conflict_not_reported_as_current_verified_state(self):
        self.add("VERIFIED_APPLIED")
        self.add("UNCERTAIN", op="rename-later")
        item = build_projections(self.journal)["workbook"]["items"][0]
        self.assertEqual(item["rename"]["effect"], "UNKNOWN")
        self.assertEqual(item["observed"]["read_state"], "UNAVAILABLE")
        self.add("BLOCKED_CONFLICT", op="rename-later")
        item = build_projections(self.journal)["workbook"]["items"][0]
        self.assertEqual(item["rename"]["effect"], "CONFLICT")

    def test_publication_failure_never_changes_action_state_and_retry_is_stable(self):
        self.add("VERIFIED_APPLIED")
        before = build_projections(self.journal, allowed_source_ids=["source-private"])
        calls = []
        def failing(key, payload):
            calls.append(key)
            raise TimeoutError("private@example.test")
        result = deliver_projection(self.journal, "html", before["html"], failing)
        self.assertEqual(result["state"], "PUBLICATION_FAILED")
        after = build_projections(self.journal, allowed_source_ids=["source-private"])
        self.assertEqual(before["html"]["operations"], after["html"]["operations"])
        self.assertEqual(before["html"]["journal_sequence"], after["html"]["journal_sequence"])
        self.assertEqual(delivery_key("html", before["html"]), delivery_key("html", after["html"]))
        self.assertNotIn("private@example.test", json.dumps(self.journal.events[-1]))
        delivered = deliver_projection(self.journal, "html", after["html"],
                                       lambda key, payload: {"verified": True})
        self.assertEqual(delivered["state"], "PUBLICATION_SUCCEEDED")
        duplicate = deliver_projection(self.journal, "html", after["html"], failing)
        self.assertEqual(duplicate["state"], "ALREADY_DELIVERED")
        self.assertEqual(len(calls), 1)

    def test_sink_requires_readback_and_freshness_is_explicit(self):
        self.add("VERIFIED_APPLIED")
        projection = build_projections(self.journal)["html"]
        self.assertIsNone(projection["stale"])
        result = deliver_projection(self.journal, "html", projection, lambda key, payload: {})
        self.assertEqual(result["state"], "PUBLICATION_FAILED")
        latest = build_projections(self.journal, current_source_head=source_head(self.journal.records()))["html"]
        self.assertFalse(latest["stale"])
        stale = build_projections(self.journal, current_source_head={"seq": 999, "hash": "x"})["html"]
        self.assertTrue(stale["stale"])

    def test_frozen_unattempted_plan_is_visible_without_fabricated_observation(self):
        ref = self.journal.evidence({"operations": [
            {"operation_id": "pending", "file_id": "source-private", "action": "MOVE",
             "direction": "FORWARD", "depends_on": [], "before": {"name": "Secret"},
             "target": {"parents": ["private-parent"]}}]})
        self.journal.append("DRAFTED", payload={"run_id": "batch-1", "batch_id": "batch-1",
                                               "proposal_hash": "a" * 64, "plan_ref": ref})
        output = build_projections(self.journal, allowed_source_ids=["source-private"])
        self.assertEqual(output["html"]["operations"][0]["state"], "NOT_ATTEMPTED")
        self.assertIsNone(output["html"]["operations"][0]["approved_plan_sha256"])
        self.assertEqual(output["html"]["items"][0]["observed"]["read_state"], "NEVER_VERIFIED")
        self.assertEqual(output["workbook"]["rows"], [])

    def test_revocation_resolves_approval_id_without_run_id(self):
        self.journal.append("APPROVED", payload={"batch_id": "batch-1", "approval_id": "a1",
                                                "proposal_hash": "a" * 64})
        self.add("VERIFIED_APPLIED")
        self.journal.append("REVOKED", payload={"approval_id": "a1"})
        operation = build_projections(self.journal)["workbook"]["operations"][0]
        self.assertEqual(operation["approval_state"], "WITHDRAWN")
        self.assertEqual(operation["state"], "VERIFIED_APPLIED")

    def test_expired_approval_is_stale_and_excluded_source_never_public(self):
        ref = self.journal.evidence({"expires_at": "2000-01-01T00:00:00Z"})
        self.journal.append("APPROVED", payload={"batch_id": "batch-1", "approval_id": "a1",
                                                "proposal_hash": "a" * 64, "approval_ref": ref})
        self.add("PREPARED")
        self.assertEqual(build_projections(self.journal)["workbook"]["operations"][0]["approval_state"], "STALE")
        self.journal.append("EXCLUDED", payload={"file_id": "source-private"})
        output = build_projections(self.journal, allowed_source_ids=["source-private"])
        self.assertEqual(output["html"]["operations"], [])
        self.assertEqual(output["html"]["items"], [])
        self.assertEqual(output["workbook"]["operations"][0]["approval_state"], "WITHDRAWN")

    def test_verified_folder_move_is_inherited_not_direct_for_descendant(self):
        folder = {"id": "folder", "name": "Folder", "mime_type": "application/vnd.google-apps.folder",
                  "parents": ["old"], "subtree": {"complete": True, "members": [{"id": "child"}]}}
        before = self.journal.evidence(folder)
        after = self.journal.evidence({**folder, "parents": ["new"]})
        self.add("VERIFIED_APPLIED", op="folder-move", action="MOVE", file_id="folder",
                 before_ref=before, intended_ref=after, observed_ref=after)
        private = build_projections(self.journal)["workbook"]
        child = next(i for i in private["items"] if i["source_id"] == "child")
        self.assertEqual(child["move"]["effect"], "NOT_TRACKED")
        self.assertEqual(child["location"]["effect"], "INHERITED")
        self.assertEqual(child["location"]["ancestor_move_operation_ids"], ["folder-move"])
        self.assertEqual(child["observed"]["read_state"], "NEVER_VERIFIED")
        self.add("VERIFIED_APPLIED", op="undo-folder", action="MOVE", file_id="folder",
                 direction="ROLLBACK", original_operation_id="folder-move", before_ref=after,
                 intended_ref=before, observed_ref=before)
        child = next(i for i in build_projections(self.journal)["workbook"]["items"] if i["source_id"] == "child")
        self.assertEqual(child["location"]["effect"], "NONE")
        self.assertEqual(child["location"]["ancestor_move_operation_ids"], [])

    def test_uncertain_folder_move_does_not_claim_inherited_success(self):
        folder = {"id": "folder", "name": "Folder", "mime_type": "application/vnd.google-apps.folder",
                  "parents": ["old"], "subtree": {"complete": True, "members": [{"id": "child"}]}}
        before = self.journal.evidence(folder)
        self.add("UNCERTAIN", op="folder-move", action="MOVE", file_id="folder",
                 before_ref=before, intended_ref=before, observed_ref=None)
        child = next(i for i in build_projections(self.journal)["workbook"]["items"] if i["source_id"] == "child")
        self.assertEqual(child["location"]["effect"], "UNKNOWN")

    def test_delivery_crash_after_sink_success_retries_same_key_without_action_change(self):
        self.add("VERIFIED_APPLIED")
        payload = build_projections(self.journal)["html"]
        calls, seen = [], set()
        def sink(key, data):
            calls.append(key)
            seen.add(key)  # A real sink reads/deduplicates its recorded key.
            return {"verified": True}
        original = self.journal.append
        def crash_on_commit(kind, **kwargs):
            if kind == "PUBLICATION_SUCCEEDED":
                raise OSError("injected journal failure")
            return original(kind, **kwargs)
        self.journal.append = crash_on_commit
        with self.assertRaises(OSError):
            deliver_projection(self.journal, "html", payload, sink)
        self.journal.append = original
        result = deliver_projection(self.journal, "html", payload, sink)
        self.assertEqual(result["state"], "PUBLICATION_SUCCEEDED")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(build_projections(self.journal)["workbook"]["operations"][0]["state"], "VERIFIED_APPLIED")

    def test_delivery_journal_failure_before_queue_prevents_sink(self):
        self.add("VERIFIED_APPLIED")
        payload = build_projections(self.journal)["html"]
        called = []
        def failure(*args, **kwargs):
            raise OSError("injected journal failure")
        self.journal.append = failure
        with self.assertRaises(OSError):
            deliver_projection(self.journal, "html", payload, lambda *args: called.append(args))
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
