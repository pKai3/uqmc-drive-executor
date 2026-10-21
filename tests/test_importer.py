import copy
import tempfile
import unittest
from pathlib import Path

from uqmc_recovery.importer import HEADERS, WORKBOOK_ID, assert_frozen_input, catalog, import_workbook, workbook_fingerprint
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, SafetyError, WITHDRAWN_ID, digest


def metadata(file_id, name="Minutes", mime="application/vnd.google-apps.document", parents=None):
    return {"id": file_id, "name": name, "mime_type": mime, "parents": ["old"] if parents is None else parents,
            "version": "1", "modified_time": "2026-10-21T00:00:00Z", "trashed": False,
            "permissions": [], "owners": [{"id": "owner"}], "drive_id": None,
            "capabilities": {"can_move": True, "can_rename": True, "can_add_children": True},
            "access": {"complete": True, "effective": []}, "subtree": {"complete": True, "members": []}}


def export_fixture():
    export = {"workbook_id": WORKBOOK_ID, "exported_at_utc": "2026-10-21T00:00:00Z",
              "complete": {tab: True for tab in HEADERS}, "sheets": {tab: [list(headers)] for tab, headers in HEADERS.items()}}
    append_row(export, "Proposed Moves", {"Proposal ID": "MOVE-001", "Decision (Yes/No)": "Yes", "Action": "Move", "Proposed target path": "Club/Archive"})
    append_row(export, "Move Manifest", {"Proposal ID": "MOVE-001", "Manifest item": "ITEM-001", "Source ID": "source", "Exact target path": "Club/Archive/Minutes"})
    append_row(export, "Proposed Renames", {"Rename ID": "RENAME-001", "Source ID": "source", "Decision (Yes/No)": "Yes", "Current name + type suffix": "Minutes.gdoc", "Proposed name + type suffix": "Committee Minutes.gdoc"})
    return export


def append_row(export, tab, values):
    export["sheets"][tab].append([values.get(h, "") for h in HEADERS[tab]])


def set_cell(export, tab, row, key, value):
    export["sheets"][tab][row][HEADERS[tab].index(key)] = value


def resolutions(export, states):
    cat = catalog(export)
    result = {}
    for ref, row in cat["records"].items():
        result[ref] = {"input_sha256": cat["input_sha256"], "row_sha256": row["row_sha256"],
                       "source_id": row["source_id"], "live_snapshot_sha256": digest(states[row["source_id"]]),
                       "depends_on_refs": [], "reviewed_checks": True}
        if row["action"] == "MOVE":
            result[ref]["target_parent_ids"] = ["new"]
        else:
            result[ref]["exact_target_name"] = "Committee Minutes"
    return result


class GuardedMetadata(dict):
    def __getitem__(self, key):
        raise AssertionError("An excluded source must not be accessed")


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name) / "state")
        self.export = export_fixture()
        self.states = {"source": metadata("source"), "old": metadata("old", "Old", FOLDER, []), "new": metadata("new", "New", FOLDER, [])}

    def tearDown(self):
        self.tmp.cleanup()

    def run_import(self, selected=("MOVE-001/ITEM-001",), **kwargs):
        arguments = {"batch_id": "batch-test", "selected_refs": selected, "metadata_by_id": self.states,
                     "resolutions": resolutions(self.export, self.states)}
        arguments.update(kwargs)
        return import_workbook(self.journal, self.export, **arguments)

    def test_move_joins_manifest_and_never_creates_approval(self):
        imported = self.run_import()
        self.assertIsNotNone(imported.plan)
        self.assertEqual(imported.plan.operations[0].file_id, "source")
        self.assertEqual(imported.plan.operations[0].target, {"parents": ["new"]})
        self.assertFalse(imported.manifest["execution_approved"])
        self.assertEqual(imported.manifest["rows"][0]["approval_state"], "NOT_APPROVED")
        self.assertNotIn("APPROVED", [r["kind"] for r in self.journal.records()])
        self.assertEqual(imported.plan.run_id, imported.plan.batch_id)

    def test_id_and_plan_hash_stable_for_same_input(self):
        first = self.run_import()
        second = self.run_import()
        self.assertEqual(first.plan.proposal_hash, second.plan.proposal_hash)
        self.assertEqual(first.plan.operations[0].operation_id, second.plan.operations[0].operation_id)

    def test_native_title_is_explicit_exact_title(self):
        imported = self.run_import(("RENAME-001",))
        self.assertEqual(imported.plan.operations[0].target, {"name": "Committee Minutes"})
        self.assertEqual(imported.plan.operations[0].before["name"], "Minutes")

    def test_display_suffix_is_rejected_without_silent_stripping(self):
        resolved = resolutions(self.export, self.states)
        resolved["RENAME-001"]["exact_target_name"] = "Committee Minutes.gdoc"
        imported = self.run_import(("RENAME-001",), resolutions=resolved)
        self.assertIsNone(imported.plan)
        self.assertEqual(imported.manifest["rows"][0]["reason_code"], "SYNTHETIC_NATIVE_EXTENSION")

    def test_uploaded_extension_preserved(self):
        self.states["source"] = metadata("source", "Report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        resolved = resolutions(self.export, self.states)
        resolved["RENAME-001"]["exact_target_name"] = "Committee Report.docx"
        self.assertIsNotNone(self.run_import(("RENAME-001",), resolutions=resolved).plan)
        resolved["RENAME-001"]["exact_target_name"] = "Committee Report"
        result = self.run_import(("RENAME-001",), resolutions=resolved)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "UPLOADED_EXTENSION_CHANGED")

    def test_verified_mime_extension_correction_is_frozen_with_proof(self):
        self.states["source"] = metadata("source", "Fixture_FA.CPR", "application/pdf")
        provenance = self.journal.evidence({"source_id": "source", "mime_type": "application/pdf", "kind": "fixture_verified_metadata"})
        proof = self.journal.evidence({"kind": "VERIFIED_EXTENSION_CORRECTION", "source_id": "source",
            "source_exact_name": "Fixture_FA.CPR", "target_exact_name": "Fixture certificate.pdf", "mime_type": "application/pdf", "provenance_ref": provenance})
        correction = {"source_suffix": ".CPR", "target_suffix": ".pdf", "mime_type": "application/pdf", "evidence_ref": proof}
        resolved = resolutions(self.export, self.states)
        resolved["RENAME-001"].update(exact_target_name="Fixture certificate.pdf", extension_correction=correction)
        result = self.run_import(("RENAME-001",), resolutions=resolved)
        self.assertIsNotNone(result.plan)
        self.assertEqual(result.plan.operations[0].extension_correction, correction)
        self.assertEqual(result.plan.operations[0].before["mime_type"], "application/pdf")

    def test_path_without_destination_id_blocks(self):
        result = self.run_import(resolutions={})
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "EXACT_TARGET_RESOLUTION_REQUIRED")

    def test_incomplete_live_metadata_blocks(self):
        del self.states["source"]["version"]
        result = self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "INCOMPLETE_METADATA")

    def test_no_selection_is_not_implicit_all(self):
        with self.assertRaisesRegex(SafetyError, "EXPLICIT_SELECTION_REQUIRED"):
            self.run_import(())

    def test_changed_workbook_invalidates_freeze(self):
        result = self.run_import()
        set_cell(self.export, "Proposed Moves", 1, "Proposed target path", "Different")
        with self.assertRaisesRegex(SafetyError, "WORKBOOK_CHANGED"):
            assert_frozen_input(result.manifest, self.export)
        next_import = self.run_import(expected_input_sha256=result.manifest["input_sha256"])
        self.assertIsNone(next_import.plan)

    def test_stale_resolution_is_blocked(self):
        resolved = resolutions(self.export, self.states)
        set_cell(self.export, "Proposed Renames", 1, "Proposed name + type suffix", "Other.gdoc")
        result = self.run_import(resolutions=resolved)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "STALE_TARGET_RESOLUTION")

    def test_no_is_persistent_record_exclusion(self):
        set_cell(self.export, "Proposed Renames", 1, "Decision (Yes/No)", "No")
        result = self.run_import(("RENAME-001",), metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "PROPOSAL_EXCLUDED")
        set_cell(self.export, "Proposed Renames", 1, "Decision (Yes/No)", "Yes")
        result = self.run_import(("RENAME-001",), metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "PROPOSAL_EXCLUDED")
        # A denied rename does not withdraw a separately permitted move of that source.
        self.assertIsNotNone(self.run_import().plan)

    def test_withdrawn_source_never_accessed_and_stays_withdrawn(self):
        set_cell(self.export, "Proposed Renames", 1, "Review state", "Withdrawn")
        result = self.run_import(metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "EXCLUDED")
        set_cell(self.export, "Proposed Renames", 1, "Review state", "")
        result = self.run_import(metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "EXCLUDED")

    def test_hard_withdrawn_id_never_accessed(self):
        set_cell(self.export, "Move Manifest", 1, "Source ID", WITHDRAWN_ID)
        result = import_workbook(self.journal, self.export, batch_id="batch", selected_refs=["MOVE-001/ITEM-001"], metadata_by_id=GuardedMetadata())
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "EXCLUDED")

    def test_partial_export_fails_closed(self):
        self.export["complete"]["Move Manifest"] = False
        with self.assertRaisesRegex(SafetyError, "WORKBOOK_RANGE_NOT_COMPLETE"):
            workbook_fingerprint(self.export)

    def test_duplicate_references_fail_closed(self):
        self.export["sheets"]["Move Manifest"].append(copy.deepcopy(self.export["sheets"]["Move Manifest"][1]))
        with self.assertRaisesRegex(SafetyError, "DUPLICATE_OR_MISSING_MANIFEST"):
            catalog(self.export)

    def test_selected_missing_row_never_silently_reduces_scope(self):
        result = self.run_import(("MOVE-001/ITEM-001", "RENAME-missing"))
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][1]["reason_code"], "SELECTED_REFERENCE_MISSING")

    def test_explicit_same_source_order_and_simulated_baseline(self):
        resolved = resolutions(self.export, self.states)
        resolved["RENAME-001"]["depends_on_refs"] = ["MOVE-001/ITEM-001"]
        result = self.run_import(("MOVE-001/ITEM-001", "RENAME-001"), resolutions=resolved)
        self.assertIsNotNone(result.plan)
        self.assertEqual(result.plan.operations[1].before["parents"], ["new"])
        self.assertEqual(result.plan.operations[1].depends_on, (result.plan.operations[0].operation_id,))

    def test_same_source_unordered_blocks(self):
        result = self.run_import(("MOVE-001/ITEM-001", "RENAME-001"))
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "UNORDERED_SAME_SOURCE")

    def test_dependency_cycle_blocks(self):
        resolved = resolutions(self.export, self.states)
        resolved["RENAME-001"]["depends_on_refs"] = ["MOVE-001/ITEM-001"]
        resolved["MOVE-001/ITEM-001"]["depends_on_refs"] = ["RENAME-001"]
        result = self.run_import(("MOVE-001/ITEM-001", "RENAME-001"), resolutions=resolved)
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "DEPENDENCY_CYCLE")

    def test_excluded_descendant_blocks_ancestor(self):
        self.states["source"] = metadata("source", "Folder", FOLDER)
        self.states["source"]["subtree"]["members"] = [{"id": WITHDRAWN_ID}]
        result = self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "EXCLUDED_SCOPE_OR_DESCENDANT")


if __name__ == "__main__":
    unittest.main()
