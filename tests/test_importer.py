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


def quarantine_fixture_rows():
    return [
        ("Proposed Moves", {"Current item": "Excluded by user", "Proposal ID": "MOVE-quarantined",
            "Action": "Move", "Status": "Previously reviewed", "Rationale": "Synthetic historical review text."}, "MOVE-quarantined"),
        ("Move Manifest", {"Current item": "Excluded by user", "Proposal ID": "MOVE-001", "Manifest item": "ITEM-quarantined",
            "Scope": "File", "Rationale": "Synthetic historical scope text."}, "MOVE-001/ITEM-quarantined"),
        ("Proposed Renames", {"Current name + type suffix": "Excluded by user", "Rename ID": "RENAME-quarantined",
            "Move proposal ID": "MOVE-001", "Manifest item": "ITEM-quarantined", "Review state": "Previously reviewed",
            "Evidence / rationale": "Synthetic historical review text."}, "RENAME-quarantined"),
    ]


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

    def test_exact_redacted_exclusions_are_not_source_records(self):
        labels = {"Proposed Moves": "Current item", "Move Manifest": "Current item",
                  "Proposed Renames": "Current name + type suffix"}
        original = catalog(self.export)
        for tab, label in labels.items():
            # Both native empty-cell representations are allowed; no identity
            # is reconstructed from neighboring rows or worksheet positions.
            values = {header: None if index % 2 else "" for index, header in enumerate(HEADERS[tab])}
            values[label] = "Excluded by user"
            append_row(self.export, tab, values)
        result = catalog(self.export)
        self.assertEqual(result["records"], original["records"])
        self.assertEqual(result["excluded_ids"], original["excluded_ids"])
        self.assertEqual(result["redacted_tombstones"], [{"tab": tab, "row": 3} for tab in labels])
        self.assertNotEqual(result["input_sha256"], original["input_sha256"])

    def test_redacted_exclusions_are_reported_and_fingerprinted_in_import(self):
        append_row(self.export, "Move Manifest", {"Current item": "Excluded by user"})
        result = self.run_import()
        self.assertIsNotNone(result.plan)
        self.assertEqual(result.manifest["redacted_tombstones"], [{"tab": "Move Manifest", "row": 3}])
        evidence = self.journal.read_evidence(result.evidence_ref)
        self.assertEqual(evidence["workbook_export"]["sheets"], self.export["sheets"])
        self.export["sheets"]["Move Manifest"].pop()
        with self.assertRaisesRegex(SafetyError, "WORKBOOK_CHANGED_REVALIDATION_REQUIRED"):
            assert_frozen_input(result.manifest, self.export)

    def test_redacted_missing_selection_blocks_without_reading_metadata(self):
        append_row(self.export, "Proposed Renames", {"Current name + type suffix": "Excluded by user"})
        result = import_workbook(self.journal, self.export, batch_id="redacted-fixture",
            selected_refs=["RENAME-redacted"], metadata_by_id=GuardedMetadata())
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "SELECTED_REFERENCE_MISSING")
        self.assertEqual(result.manifest["redacted_tombstones"], [{"tab": "Proposed Renames", "row": 3}])

    def test_redaction_marker_with_identity_reference_or_action_fails_closed(self):
        cases = [("Proposed Moves", "Current item", "Action", "Move"),
                 ("Move Manifest", "Current item", "Proposal ID", "MOVE-extra"),
                 ("Move Manifest", "Current item", "Manifest item", "ITEM-extra"),
                 ("Move Manifest", "Current item", "Source ID", "fixture-redacted-source"),
                 ("Proposed Renames", "Current name + type suffix", "Rename ID", "RENAME-extra"),
                 ("Proposed Renames", "Current name + type suffix", "Source ID", "fixture-redacted-source"),
                 ("Proposed Renames", "Current name + type suffix", "Decision (Yes/No)", "Yes")]
        for tab, label, other, value in cases:
            with self.subTest(tab=tab, other=other):
                export = copy.deepcopy(self.export)
                append_row(export, tab, {label: "Excluded by user", other: value})
                with self.assertRaisesRegex(SafetyError, "AMBIGUOUS_REDACTED_EXCLUSION"):
                    catalog(export)

    def test_redaction_marker_in_populated_record_cannot_restore_eligibility(self):
        for tab, label in (("Proposed Moves", "Current item"), ("Move Manifest", "Current item"),
                           ("Proposed Renames", "Current name + type suffix")):
            with self.subTest(tab=tab):
                export = copy.deepcopy(self.export)
                set_cell(export, tab, 1, label, "Excluded by user")
                with self.assertRaisesRegex(SafetyError, "AMBIGUOUS_REDACTED_EXCLUSION"):
                    import_workbook(self.journal, export, batch_id="ambiguous-fixture",
                        selected_refs=["MOVE-001/ITEM-001"], metadata_by_id=GuardedMetadata())

    def test_redacted_exclusion_requires_exact_label_and_empty_other_cells(self):
        for tab, label in (("Proposed Moves", "Current item"), ("Move Manifest", "Current item"),
                           ("Proposed Renames", "Current name + type suffix")):
            for marker in ("excluded by user", " Excluded by user", "Excluded by user "):
                with self.subTest(tab=tab, marker=marker):
                    export = copy.deepcopy(self.export)
                    append_row(export, tab, {label: marker})
                    with self.assertRaisesRegex(SafetyError, "DUPLICATE_OR_MISSING"):
                        catalog(export)
            for value in (" ", False, 0, [], {}):
                with self.subTest(tab=tab, extra=value):
                    export = copy.deepcopy(self.export)
                    export["sheets"][tab][0].append("Extra field")
                    append_row(export, tab, {label: "Excluded by user"})
                    export["sheets"][tab][-1].append(value)
                    with self.assertRaisesRegex(SafetyError, "AMBIGUOUS_REDACTED_EXCLUSION"):
                        catalog(export)

    def test_unidentified_rows_without_exact_tombstone_still_fail(self):
        for tab, values in (("Proposed Moves", {"Rationale": "Excluded by user"}),
                            ("Move Manifest", {"Source ID": "fixture-source-only"}),
                            ("Proposed Renames", {"Proposed name + type suffix": "Excluded by user"})):
            with self.subTest(tab=tab):
                export = copy.deepcopy(self.export)
                append_row(export, tab, values)
                with self.assertRaisesRegex(SafetyError, "DUPLICATE_OR_MISSING"):
                    catalog(export)

    def test_reserved_row_header_cannot_hide_nonempty_tombstone_data(self):
        self.export["sheets"]["Move Manifest"][0].append("_row")
        append_row(self.export, "Move Manifest", {"Current item": "Excluded by user"})
        self.export["sheets"]["Move Manifest"][-1].append("fixture-hidden-identity")
        with self.assertRaisesRegex(SafetyError, "INVALID_WORKBOOK_HEADER"):
            catalog(self.export)

    def test_reference_quarantine_allows_unrelated_batch_and_persists_before_metadata(self):
        rows = quarantine_fixture_rows()
        for tab, values, _ in rows:
            append_row(self.export, tab, values)
        original_ids = set(catalog(self.export)["records"])
        expected = {ref for _, _, ref in rows}
        journal, testcase = self.journal, self

        class AfterQuarantineMetadata(dict):
            def __getitem__(self, key):
                recorded = {event["payload"].get("proposal_ref") for event in journal.records()
                            if event["kind"] == "PROPOSAL_EXCLUDED"}
                testcase.assertTrue(expected <= recorded)
                return super().__getitem__(key)

        result = self.run_import(metadata_by_id=AfterQuarantineMetadata(self.states))
        self.assertIsNotNone(result.plan)
        self.assertEqual(original_ids, {"MOVE-001/ITEM-001", "RENAME-001"})
        self.assertEqual(result.manifest["quarantined_tombstones"],
            [{"tab": tab, "row": 3, "refs": [ref]} for tab, _, ref in rows])
        events = [event for event in self.journal.records() if event["kind"] == "PROPOSAL_EXCLUDED"]
        self.assertEqual({event["payload"]["proposal_ref"] for event in events}, expected)
        self.assertTrue(all(event["payload"]["reason_code"] == "WORKBOOK_REDACTED_EXCLUSION" for event in events))
        evidence = self.journal.read_evidence(result.evidence_ref)
        self.assertEqual(evidence["workbook_export"]["sheets"], self.export["sheets"])

    def test_quarantined_references_cannot_be_selected_or_resolved(self):
        for tab, values, _ in quarantine_fixture_rows():
            append_row(self.export, tab, values)
        result = import_workbook(self.journal, self.export, batch_id="quarantined-selection",
            selected_refs=[ref for _, _, ref in quarantine_fixture_rows()], metadata_by_id=GuardedMetadata())
        self.assertIsNone(result.plan)
        self.assertTrue(all(row["reason_code"] == "PROPOSAL_EXCLUDED" for row in result.manifest["rows"]))
        self.assertTrue(all(row["approval_state"] == "WITHDRAWN" for row in result.manifest["rows"]))

    def test_quarantined_parent_proposal_blocks_later_manifest_even_after_marker_removed(self):
        tab, values, ref = quarantine_fixture_rows()[0]
        append_row(self.export, tab, values)
        self.assertIsNotNone(self.run_import().plan)
        self.export["sheets"][tab].pop()
        append_row(self.export, tab, {"Proposal ID": ref, "Decision (Yes/No)": "Yes", "Action": "Move"})
        append_row(self.export, "Move Manifest", {"Proposal ID": ref, "Manifest item": "ITEM-later", "Source ID": "fixture-later"})
        result = import_workbook(self.journal, self.export, batch_id="later-parent",
            selected_refs=[ref + "/ITEM-later"], metadata_by_id=GuardedMetadata())
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "PROPOSAL_EXCLUDED")

    def test_quarantined_manifest_cannot_reopen_and_does_not_exclude_sibling(self):
        tab, values, ref = quarantine_fixture_rows()[1]
        append_row(self.export, tab, values)
        self.assertIsNotNone(self.run_import().plan)
        self.export["sheets"][tab].pop()
        append_row(self.export, tab, {"Proposal ID": "MOVE-001", "Manifest item": "ITEM-quarantined", "Source ID": "fixture-later"})
        result = import_workbook(self.journal, self.export, batch_id="later-manifest", selected_refs=[ref], metadata_by_id=GuardedMetadata())
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "PROPOSAL_EXCLUDED")
        self.assertIsNotNone(import_workbook(self.journal, self.export, batch_id="unrelated-sibling",
            selected_refs=["MOVE-001/ITEM-001"], metadata_by_id=self.states,
            resolutions={"MOVE-001/ITEM-001": resolutions(export_fixture(), self.states)["MOVE-001/ITEM-001"] |
                {"input_sha256": catalog(self.export)["input_sha256"]}}).plan)

    def test_quarantined_rename_cannot_reopen_after_identity_and_yes_restored(self):
        tab, values, ref = quarantine_fixture_rows()[2]
        append_row(self.export, tab, values)
        self.assertIsNotNone(self.run_import().plan)
        self.export["sheets"][tab].pop()
        append_row(self.export, tab, {"Rename ID": ref, "Source ID": "fixture-later",
            "Decision (Yes/No)": "Yes", "Proposed name + type suffix": "Later.pdf"})
        result = import_workbook(self.journal, self.export, batch_id="later-rename", selected_refs=[ref], metadata_by_id=GuardedMetadata())
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "PROPOSAL_EXCLUDED")

    def test_reference_quarantine_rejects_source_or_target_fields(self):
        forbidden = {"Proposed Moves": ("Source path", "Proposed target path"),
                     "Move Manifest": ("Source ID", "Source path", "Exact target path"),
                     "Proposed Renames": ("Source ID", "Current source path", "Proposed destination folder", "Proposed name + type suffix")}
        for tab, values, _ in quarantine_fixture_rows():
            for field in forbidden[tab]:
                with self.subTest(tab=tab, field=field):
                    export = copy.deepcopy(self.export)
                    append_row(export, tab, values | {field: "fixture-must-not-be-resolved"})
                    with self.assertRaisesRegex(SafetyError, "AMBIGUOUS_REDACTED_EXCLUSION"):
                        catalog(export)
            with self.subTest(tab=tab, field="unknown field"):
                export = copy.deepcopy(self.export)
                export["sheets"][tab][0].append("Extra source identity")
                append_row(export, tab, values)
                export["sheets"][tab][-1].append("fixture-must-not-be-resolved")
                with self.assertRaisesRegex(SafetyError, "AMBIGUOUS_REDACTED_EXCLUSION"):
                    catalog(export)

    def test_reference_quarantine_requires_every_exact_primary_reference(self):
        refs = {"Proposed Moves": ("Proposal ID",), "Move Manifest": ("Proposal ID", "Manifest item"),
                "Proposed Renames": ("Rename ID", "Move proposal ID", "Manifest item")}
        for tab, values, _ in quarantine_fixture_rows():
            for field in refs[tab]:
                for invalid in (None, "", " ", " padded ", False, 1):
                    with self.subTest(tab=tab, field=field, invalid=invalid):
                        export = copy.deepcopy(self.export)
                        append_row(export, tab, values | {field: invalid})
                        with self.assertRaisesRegex(SafetyError, "AMBIGUOUS_REDACTED_EXCLUSION"):
                            catalog(export)

    def test_quarantined_rename_allows_exact_unresolved_review_placeholder(self):
        tab, values, ref = quarantine_fixture_rows()[2]
        values["Proposed destination folder"] = "Pending restricted-destination review"
        append_row(self.export, tab, values)
        source = catalog(self.export)
        self.assertNotIn(ref, source["records"])
        self.assertIn(ref, source["excluded_refs"])
        self.assertEqual(source["quarantined_tombstones"], [{"tab": tab, "row": 3, "refs": [ref]}])
        result = self.run_import()
        self.assertIsNotNone(result.plan)
        self.assertTrue(any(event["kind"] == "PROPOSAL_EXCLUDED" and
            event["payload"].get("proposal_ref") == ref for event in self.journal.records()))
        raw = self.journal.read_evidence(result.evidence_ref)["workbook_export"]
        self.assertEqual(raw, self.export)

    def test_quarantined_review_placeholder_cannot_mask_other_target_or_source_data(self):
        tab, values, _ = quarantine_fixture_rows()[2]
        sentinel = "Pending restricted-destination review"
        changes = [{"Proposed destination folder": value} for value in
            (sentinel + " ", sentinel.lower(), "Fixture/resolved-destination")]
        changes += [{"Proposed destination folder": sentinel, field: "fixture-not-empty"}
                    for field in ("Source ID", "Current source path", "Proposed name + type suffix")]
        changes += [{"Proposed destination folder": sentinel, "Rename ID": ""}]
        for change in changes:
            with self.subTest(change=change):
                export = copy.deepcopy(self.export)
                append_row(export, tab, values | change)
                with self.assertRaisesRegex(SafetyError, "AMBIGUOUS_REDACTED_EXCLUSION"):
                    catalog(export)

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
