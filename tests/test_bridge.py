import copy
from pathlib import Path
import tempfile
import unittest
import uuid

from uqmc_recovery.adapters import fixture_state
from uqmc_recovery.bridge import METADATA_FIELDS, READ_TOOL, ingest_receipt, prepare_reads, reconcile_reads
from uqmc_recovery.journal import Journal, JournalCorrupt
from uqmc_recovery.model import FOLDER, SafetyError, WITHDRAWN_ID, digest


def plan_body():
    return {"run_id": "test-run", "batch_id": "test-batch", "proposal_revision": "r1",
            "excluded_ids": [], "operations": [
                {"operation_id": "move", "file_id": "source", "action": "MOVE",
                 "before": fixture_state("source", "Budget", "application/vnd.google-apps.spreadsheet"),
                 "target": {"parents": ["destination"]}, "proposal_refs": ["MOVE-TEST"]},
                {"operation_id": "rename", "file_id": "source", "action": "RENAME",
                 "before": fixture_state("source", "Budget", "application/vnd.google-apps.spreadsheet", ["destination"]),
                 "target": {"name": "Club Budget"}, "proposal_refs": ["RENAME-TEST"]}]}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / "state")

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, body=None):
        return prepare_reads(self.journal, body or plan_body(), set())

    def test_exact_read_only_arguments_deduplicate_ids_and_freeze_plan(self):
        body = plan_body()
        bundle = self.prepare(body)
        self.assertEqual(bundle["expected_file_ids"], ["destination", "origin", "source"])
        self.assertEqual(bundle["plan_hash"], digest(body))
        self.assertEqual(self.journal.read_evidence(bundle["plan_ref"]), body)
        uuid.UUID(bundle["request_id"])
        for request in bundle["requests"]:
            self.assertEqual(request["tool"], READ_TOOL)
            self.assertEqual(request["args"], {"fileId": request["file_id"],
                                               "fields": METADATA_FIELDS, "supportsAllDrives": True})
        body["operations"][0]["file_id"] = "changed"
        self.assertEqual(self.journal.read_evidence(bundle["plan_ref"])["operations"][0]["file_id"], "source")
        self.assertFalse(bundle["execution_ready"])

    def test_denied_source_destination_and_original_parent_never_issued(self):
        for file_id in ("source", "destination", "origin"):
            with self.subTest(file_id=file_id), self.assertRaisesRegex(SafetyError, "EXCLUDED"):
                prepare_reads(self.journal, plan_body(), {file_id})
        self.assertEqual(self.journal.records(), [])
        self.assertEqual(list(self.journal.evidence_dir.iterdir()), [])

    def test_journal_and_plan_tombstones_apply_before_read_preparation(self):
        self.journal.append("EXCLUDED", payload={"file_id": "source"})
        with self.assertRaisesRegex(SafetyError, "EXCLUDED"):
            self.prepare()
        other = Journal(Path(self.temp.name) / "other")
        body = plan_body()
        body["excluded_ids"] = ["destination"]
        with self.assertRaisesRegex(SafetyError, "EXCLUDED"):
            prepare_reads(other, body, set())
        body["excluded_ids"] = []
        other.append("PROPOSAL_EXCLUDED", payload={"proposal_ref": "MOVE-TEST"})
        with self.assertRaisesRegex(SafetyError, "PROPOSAL_EXCLUDED"):
            prepare_reads(other, body, set())

    def test_withdrawn_fixture_sentinel_and_descendant_remain_excluded(self):
        body = plan_body()
        body["operations"][0]["before"]["subtree"]["members"] = [{"id": WITHDRAWN_ID}]
        with self.assertRaisesRegex(SafetyError, "EXCLUDED_DESCENDANT"):
            self.prepare(body)
        body = plan_body()
        body["operations"][0]["file_id"] = WITHDRAWN_ID
        body["operations"][0]["before"]["id"] = WITHDRAWN_ID
        with self.assertRaisesRegex(SafetyError, "EXCLUDED"):
            self.prepare(body)

    def test_raw_response_id_mismatch_and_unrequested_id_rejected(self):
        bundle = self.prepare()
        with self.assertRaisesRegex(SafetyError, "ID_MISMATCH"):
            ingest_receipt(self.journal, bundle["request_id"], "source", {"id": "wrong"})
        with self.assertRaisesRegex(SafetyError, "NOT_REQUESTED"):
            ingest_receipt(self.journal, bundle["request_id"], "unrequested", {"id": "unrequested"})
        self.assertEqual([r["kind"] for r in self.journal.records()], ["READ_PREPARED"])

    def test_receipt_before_prepare_is_rejected_without_raw_error_leak(self):
        with self.assertRaisesRegex(SafetyError, "READ_REQUEST_UNKNOWN") as raised:
            ingest_receipt(self.journal, str(uuid.uuid4()), "source", {"id": "source", "secret": "private-error"})
        self.assertNotIn("private-error", str(raised.exception))

    def test_out_of_order_receipts_and_complete_fixture_remain_read_only(self):
        bundle = self.prepare()
        for file_id in ("source", "origin", "destination"):
            raw = fixture_state(file_id, "Literal title", FOLDER if file_id != "source" else "application/pdf")
            ingest_receipt(self.journal, bundle["request_id"], file_id, raw)
        report = reconcile_reads(self.journal, bundle["request_id"])
        self.assertEqual(report["missing_file_ids"], [])
        self.assertEqual(report["state"], "READ_ONLY_METADATA_COMPLETE")
        self.assertTrue(all(item["valid_snapshot"] for item in report["assessments"].values()))
        self.assertFalse(report["execution_ready"])
        self.assertTrue({r["kind"] for r in self.journal.records()} <= {"READ_PREPARED", "READ_RECEIVED", "READ_RECONCILED"})

    def test_incomplete_connector_metadata_blocks_even_with_all_receipts(self):
        bundle = self.prepare()
        for file_id in bundle["expected_file_ids"]:
            raw = {"structuredContent": {"id": file_id, "title": "Budget", "mime_type": "application/pdf",
                                          "parent_ids": ["origin"], "permissions": []}, "isError": False}
            ingest_receipt(self.journal, bundle["request_id"], file_id, raw)
        report = reconcile_reads(self.journal, bundle["request_id"])
        self.assertEqual(report["state"], "READ_ONLY_BLOCKED")
        self.assertIn("INCOMPLETE_METADATA_OR_ACCESS", report["reason_codes"])
        self.assertIn("version", report["assessments"]["source"]["assessment"]["missing_fields"])

    def test_identical_duplicate_is_idempotent_different_receipt_blocked(self):
        bundle = self.prepare()
        receipt_id = str(uuid.uuid4())
        raw = {"id": "source", "title": "First"}
        first = ingest_receipt(self.journal, bundle["request_id"], "source", raw, receipt_id)
        second = ingest_receipt(self.journal, bundle["request_id"], "source", copy.deepcopy(raw), receipt_id)
        self.assertEqual(first, second)
        self.assertEqual(len([r for r in self.journal.records() if r["kind"] == "READ_RECEIVED"]), 1)
        with self.assertRaisesRegex(SafetyError, "CONFLICT"):
            ingest_receipt(self.journal, bundle["request_id"], "source", {"id": "source", "title": "Later"})
        with self.assertRaisesRegex(SafetyError, "UUID_CONFLICT"):
            ingest_receipt(self.journal, bundle["request_id"], "origin", {"id": "origin"}, receipt_id)
        later_id = str(uuid.uuid4())
        ingest_receipt(self.journal, bundle["request_id"], "origin", {"id": "origin"}, later_id)
        with self.assertRaisesRegex(SafetyError, "UUID_CONFLICT"):
            ingest_receipt(self.journal, bundle["request_id"], "source", raw, later_id)

    def test_explicit_withdrawn_operation_is_not_read(self):
        for field, value in (("excluded", True), ("withdrawn", True), ("status", "WITHDRAWN")):
            body = plan_body()
            body["operations"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(SafetyError, "EXCLUDED"):
                self.prepare(body)
        self.assertEqual(self.journal.records(), [])

    def test_tampered_receipt_and_frozen_plan_fail_closed(self):
        bundle = self.prepare()
        ref = ingest_receipt(self.journal, bundle["request_id"], "source", {"id": "source"})
        (self.journal.evidence_dir / (ref[7:] + ".json")).write_text("{}")
        with self.assertRaises(JournalCorrupt):
            reconcile_reads(self.journal, bundle["request_id"])
        (self.journal.evidence_dir / (bundle["plan_ref"][7:] + ".json")).write_text("{}")
        with self.assertRaises(JournalCorrupt):
            ingest_receipt(self.journal, bundle["request_id"], "origin", {"id": "origin"})

    def test_exclusion_after_prepare_blocks_import_and_reconciliation(self):
        bundle = self.prepare()
        ingest_receipt(self.journal, bundle["request_id"], "source", {"id": "source"})
        self.journal.append("EXCLUDED", payload={"file_id": "destination"})
        with self.assertRaisesRegex(SafetyError, "EXCLUDED"):
            ingest_receipt(self.journal, bundle["request_id"], "origin", {"id": "origin"})
        report = reconcile_reads(self.journal, bundle["request_id"])
        self.assertIn("EXCLUDED_AFTER_PREPARE", report["reason_codes"])
        self.assertEqual(report["state"], "READ_ONLY_BLOCKED")

    def test_missing_receipts_and_tool_errors_are_not_success(self):
        bundle = self.prepare()
        with self.assertRaisesRegex(SafetyError, "NOT_METADATA") as raised:
            ingest_receipt(self.journal, bundle["request_id"], "source",
                           {"isError": True, "private_detail": "private-provider-error"})
        self.assertNotIn("private-provider-error", str(raised.exception))
        report = reconcile_reads(self.journal, bundle["request_id"])
        self.assertEqual(report["missing_file_ids"], bundle["expected_file_ids"])
        self.assertIn("MISSING_RECEIPTS", report["reason_codes"])
        self.assertFalse(report["execution_ready"])

    def test_provider_mutation_claims_never_become_action_events(self):
        bundle = self.prepare()
        raw = {"id": "source", "kind": "VERIFIED_APPLIED", "approved": True, "execution_ready": True}
        ingest_receipt(self.journal, bundle["request_id"], "source", raw)
        report = reconcile_reads(self.journal, bundle["request_id"])
        self.assertFalse(report["execution_ready"])
        self.assertTrue({r["kind"] for r in self.journal.records()} <= {"READ_PREPARED", "READ_RECEIVED", "READ_RECONCILED"})

    def test_urls_or_pseudo_root_are_not_accepted_as_stable_ids(self):
        for value in ("root", "https://example.invalid/file", "source,target", " spaced "):
            body = plan_body()
            body["operations"][0]["file_id"] = value
            with self.subTest(value=value), self.assertRaisesRegex(SafetyError, "STABLE_ID"):
                self.prepare(body)


if __name__ == "__main__":
    unittest.main()
