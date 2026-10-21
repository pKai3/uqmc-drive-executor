import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_importer import GuardedMetadata, append_row, export_fixture, metadata, resolutions, set_cell
from uqmc_recovery.importer import catalog, import_workbook
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, SafetyError, digest
from uqmc_recovery.section_importer import import_section, validate_section_authority


class SectionImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / "state")
        self.export = export_fixture()
        set_cell(self.export, "Proposed Moves", 1, "Decision (Yes/No)", "")
        self.states = {"source": metadata("source"), "old": metadata("old", "Old", FOLDER, []),
                       "new": metadata("new", "Fresh", FOLDER, ["target-section"]),
                       "target-section": metadata("target-section", "Target", FOLDER, [])}
        self.projection = {"schema_version": "uqmc.destination.section.v1", "section_id": "fixture-section",
            "projection_revision": "revision-1", "model_revision": "model-v2", "workbook_id": self.export["workbook_id"],
            "input_sha256": catalog(self.export)["input_sha256"], "complete": True,
            "method_profile": "new_destination_individual_files", "allowed_actions": ["MOVE"],
            "allowed_destination_parent_ids": ["new"], "declared_member_count": 1,
            "members": [{"source_id": "source", "direct_operation_refs": ["MOVE-001/ITEM-001"], "ancestor_move_operation_refs": []}],
            "operations": [{"reference": "MOVE-001/ITEM-001", "source_id": "source", "target_parent_ids": ["new"], "depends_on_refs": [], "target_logical_key": "fresh"}]}
        self.projection["source_inventory_evidence_ref"] = self.journal.evidence({
            "schema_version": "uqmc.section.source-inventory.v1", "section_id": "fixture-section",
            "complete": True, "source_container_ids_complete": True, "source_file_ids": ["source"], "source_container_ids": ["old"]})
        requirement = {"logical_key": "fresh", "logical_path": "Target/Fresh", "folder_name": "Fresh",
                       "parent_section_id": "target-section", "kind": "NEW_FOLDER_REQUIRED", "status": "VERIFIED_CREATED", "created_folder_id": "new"}
        binding = {key: requirement[key] for key in ("logical_key", "logical_path", "folder_name", "parent_section_id")}
        authority = self.journal.evidence({"schema_version": "uqmc.folder.creation.authorization.v1", "decision": "APPROVED",
                    "actor": "fixture-owner", "owner_instruction_evidence_reference": "fixture-owner-message", **binding})
        request = self.journal.evidence({"action": "CREATE_FOLDER", **binding})
        response = self.journal.evidence({"id": "new"})
        readback = self.journal.evidence(self.states["new"])
        receipt = {"schema_version": "uqmc.folder.creation.receipt.v1", "state": "VERIFIED_CREATED", "request_terminal": True,
                   "created_folder_id": "new", **binding, "creation_authorization_ref": authority,
                   "request_ref": request, "response_ref": response, "readback_ref": readback}
        requirement["creation_evidence_ref"] = self.journal.evidence(receipt)
        self.projection["destination_requirements"] = [requirement]
        spec_ref = self.journal.evidence({"schema_version": "uqmc.section.destination-spec.v1", "section_id": "fixture-section",
                                         "model_revision": "model-v2", "complete": True,
                                         "destinations": [{key: requirement[key] for key in ("logical_key", "logical_path", "folder_name", "parent_section_id", "kind")}]})
        self.review_context_ref = self.journal.evidence({"schema_version": "uqmc.section.review-context.v1", "section_id": "fixture-section",
            "model_revision": "model-v2", "source_inventory_evidence_ref": self.projection["source_inventory_evidence_ref"],
            "destination_spec_evidence_ref": spec_ref, "reviewed_by": "fixture-reviewer", "reviewed_at_utc": "2026-01-01T00:00:00Z",
            "provenance": "independently_reviewed_corrected_model"})

    def tearDown(self):
        self.temp.cleanup()

    def approval(self, projection=None, **overrides):
        projection = projection or self.projection
        body = {"schema_version": "uqmc.section.owner-approval.v1", "decision": "APPROVED", "actor": "fixture-owner",
                "approval_id": "section-" + digest(projection)[:16], "approved_at_utc": "2026-01-01T00:00:00Z",
                "provenance": {"kind": "explicit_owner_instruction", "evidence_reference": "fixture-message"},
                "section_id": projection["section_id"], "method_profile": projection["method_profile"],
                "projection_revision": projection["projection_revision"], "projection_sha256": digest(projection),
                "member_source_ids": [m["source_id"] for m in projection["members"]],
                "operation_refs": [op["reference"] for op in projection["operations"]], "allowed_actions": ["MOVE"],
                "allowed_destination_parent_ids": projection["allowed_destination_parent_ids"]}
        body.update(overrides)
        return self.journal.evidence(body)

    def run_section(self, **overrides):
        args = {"projection": self.projection, "owner_approval_ref": self.approval(), "expected_section_id": "fixture-section",
                "batch_id": "section-batch", "metadata_by_id": self.states, "resolutions": resolutions(self.export, self.states),
                "review_context_ref": self.review_context_ref}
        args.update(overrides)
        return import_section(self.journal, self.export, **args)

    def test_blank_rows_allowed_only_by_explicit_section(self):
        base = import_workbook(self.journal, self.export, batch_id="plain", selected_refs=["MOVE-001/ITEM-001"], metadata_by_id=self.states, resolutions=resolutions(self.export, self.states))
        self.assertIsNone(base.plan)
        result = self.run_section()
        self.assertIsNotNone(result.plan)
        self.assertEqual(result.plan.operations[0].action, "MOVE")
        self.assertTrue(result.manifest["section_approval_validated"])
        self.assertFalse(result.manifest["execution_approved"])
        self.assertEqual(result.manifest["rows"][0]["state"], "ELIGIBLE_PENDING_EXACT_BATCH_BINDING")
        self.assertEqual(result.manifest["rows"][0]["approval_basis"], "SECTION_OWNER_INSTRUCTION")
        self.assertTrue(result.plan.proposal_revision.startswith("section:"))
        self.assertNotIn("APPROVED", [r["kind"] for r in self.journal.records()])

    def test_section_authority_revalidates_exact_import_origin(self):
        result = self.run_section()
        authority = validate_section_authority(self.journal, result.plan)
        self.assertEqual(authority["section_manifest_ref"], result.evidence_ref)
        self.assertEqual(authority["proposal_hash"], result.plan.proposal_hash)
        self.assertEqual(authority["owner_approval_id"], result.manifest["authorization_scope"]["owner_approval_id"])

    def test_section_authority_blocks_later_revocation(self):
        result = self.run_section()
        approval_id = result.manifest["authorization_scope"]["owner_approval_id"]
        self.journal.append("SECTION_APPROVAL_REVOKED", payload={"approval_id": approval_id})
        with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_REVOKED_OR_SUPERSEDED"):
            validate_section_authority(self.journal, result.plan)

    def test_section_authority_blocks_later_supersession(self):
        result = self.run_section()
        approval_id = result.manifest["authorization_scope"]["owner_approval_id"]
        self.journal.append("SECTION_APPROVAL_SUPERSEDED", payload={"approval_id": approval_id})
        with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_REVOKED_OR_SUPERSEDED"):
            validate_section_authority(self.journal, result.plan)

    def test_section_authority_blocks_expiry_after_import(self):
        result = self.run_section(owner_approval_ref=self.approval(expires_at_utc="2999-01-01T00:00:00Z"))
        with patch("uqmc_recovery.section_importer.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(3000, 1, 1, tzinfo=timezone.utc)
            with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_EXPIRED"):
                validate_section_authority(self.journal, result.plan)

    def test_section_authority_rejects_unrecorded_candidate(self):
        result = self.run_section()
        changed = replace(result.plan, proposal_revision="section:unrecorded")
        with self.assertRaisesRegex(SafetyError, "SECTION_PLAN_ORIGIN_REQUIRED"):
            validate_section_authority(self.journal, changed)

    def test_non_section_fixture_has_no_section_authority(self):
        set_cell(self.export, "Proposed Moves", 1, "Decision (Yes/No)", "Yes")
        result = import_workbook(self.journal, self.export, batch_id="plain", selected_refs=["MOVE-001/ITEM-001"],
            metadata_by_id=self.states, resolutions=resolutions(self.export, self.states))
        self.assertIsNotNone(result.plan)
        self.assertIsNone(validate_section_authority(self.journal, result.plan))

    def test_arbitrary_eligibility_refs_cannot_bypass(self):
        with self.assertRaisesRegex(SafetyError, "INVALID_SECTION_ELIGIBILITY"):
            import_workbook(self.journal, self.export, batch_id="plain", selected_refs=["MOVE-001/ITEM-001"], metadata_by_id=self.states,
                            resolutions={}, _section_eligibility={"MOVE-001/ITEM-001"})

    def test_no_remains_excluded_under_section_approval(self):
        set_cell(self.export, "Proposed Moves", 1, "Decision (Yes/No)", "No")
        self.projection["input_sha256"] = catalog(self.export)["input_sha256"]
        result = self.run_section(metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["section_reason_code"], "SECTION_CONTAINS_EXCLUDED_MOVE_RECORD")

    def test_source_withdrawal_remains_excluded(self):
        self.journal.append("EXCLUDED", payload={"file_id": "source"})
        result = self.run_section(metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["section_reason_code"], "SECTION_CONTAINS_EXCLUDED_SOURCE_OR_TARGET")

    def test_changed_projection_rejects_old_approval(self):
        approval_ref = self.approval()
        self.projection["projection_revision"] = "revision-2"
        with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_STALE"):
            self.run_section(owner_approval_ref=approval_ref)

    def test_unrelated_section_rejected(self):
        with self.assertRaisesRegex(SafetyError, "UNRELATED_SECTION"):
            self.run_section(expected_section_id="another-section")

    def test_move_only_does_not_accept_rename_scope(self):
        self.projection["allowed_actions"] = ["MOVE", "RENAME"]
        with self.assertRaisesRegex(SafetyError, "SECTION_MOVE_ONLY"):
            self.run_section()

    def test_unresolved_new_destination_has_no_fabricated_id(self):
        requirement = self.projection["destination_requirements"][0]
        requirement.update(status="UNRESOLVED", created_folder_id=None, creation_evidence_ref=None)
        self.projection["allowed_destination_parent_ids"] = []
        self.projection["operations"][0]["target_parent_ids"] = []
        result = self.run_section(metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["section_reason_code"], "NEW_DESTINATION_CREATION_REQUIRED")
        self.assertFalse(any(r["kind"] in {"ATTEMPTED", "PREPARED"} for r in self.journal.records()))

    def test_uncertain_creation_blocks_without_retry(self):
        requirement = self.projection["destination_requirements"][0]
        requirement.update(status="UNCERTAIN", created_folder_id=None, creation_evidence_ref=None)
        self.projection["allowed_destination_parent_ids"] = []
        self.projection["operations"][0]["target_parent_ids"] = []
        result = self.run_section(metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertEqual(result.manifest["section_reason_code"], "DESTINATION_CREATION_UNCERTAIN_NO_RETRY")

    def test_source_container_cannot_be_reused_as_destination(self):
        self.projection["destination_requirements"][0]["created_folder_id"] = "old"
        self.projection["allowed_destination_parent_ids"] = ["old"]
        self.projection["operations"][0]["target_parent_ids"] = ["old"]
        result = self.run_section()
        self.assertEqual(result.manifest["section_reason_code"], "ORIGINAL_CONTAINER_REUSED_AS_DESTINATION")

    def test_viewer_node_source_identity_is_not_destination_identity(self):
        self.projection["destination_requirements"][0]["sourceId"] = "old"
        result = self.run_section()
        self.assertEqual(result.manifest["section_reason_code"], "DISPLAY_OR_SOURCE_ID_IS_NOT_DESTINATION_ID")

    def test_individual_profile_rejects_folder_move(self):
        self.states["source"]["mime_type"] = FOLDER
        result = self.run_section()
        self.assertEqual(result.manifest["section_reason_code"], "INDIVIDUAL_FILES_FORBID_SOURCE_FOLDER_MOVES")

    def test_individual_profile_rejects_inherited_shortcut(self):
        self.projection["members"][0]["ancestor_move_operation_refs"] = ["MOVE-001/ITEM-001"]
        result = self.run_section()
        self.assertEqual(result.manifest["section_reason_code"], "INDIVIDUAL_FILES_FORBID_INHERITED_SHORTCUTS")

    def test_count_alone_cannot_establish_expected_file_set(self):
        self.projection["source_inventory_evidence_ref"] = self.journal.evidence({
            "schema_version": "uqmc.section.source-inventory.v1", "section_id": "fixture-section", "complete": True,
            "source_container_ids_complete": True, "source_file_ids": ["different-file"], "source_container_ids": ["old"]})
        result = self.run_section()
        self.assertEqual(result.manifest["section_reason_code"], "INDEPENDENT_SOURCE_INVENTORY_BINDING_MISMATCH")

    def test_logical_name_receipt_mismatch_blocks(self):
        self.projection["destination_requirements"][0]["folder_name"] = "Other"
        result = self.run_section()
        self.assertEqual(result.manifest["section_reason_code"], "INDEPENDENT_DESTINATION_SPEC_BINDING_MISMATCH")

    def test_missing_independent_review_context_blocks_before_metadata(self):
        result = self.run_section(review_context_ref=None, metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertEqual(result.manifest["section_reason_code"], "INDEPENDENT_MODEL_REVIEW_CONTEXT_REQUIRED")

    def test_projection_cannot_select_its_own_target_section(self):
        self.projection["destination_requirements"][0]["parent_section_id"] = "wrong-section"
        result = self.run_section(metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertEqual(result.manifest["section_reason_code"], "INDEPENDENT_DESTINATION_SPEC_BINDING_MISMATCH")

    def test_target_section_exclusion_blocks_before_metadata(self):
        self.journal.append("EXCLUDED", payload={"file_id": "target-section"})
        result = self.run_section(metadata_by_id=GuardedMetadata(), resolutions={})
        self.assertEqual(result.manifest["section_reason_code"], "SECTION_CONTAINS_EXCLUDED_SOURCE_OR_TARGET")

    def test_missing_resolution_never_silently_reduces_selection(self):
        result = self.run_section(resolutions={})
        self.assertIsNone(result.plan)
        self.assertFalse(result.manifest["plan_ready_for_exact_batch_binding"])
        self.assertEqual(result.manifest["rows"][0]["reason_code"], "EXACT_TARGET_RESOLUTION_REQUIRED")

    def test_changed_destination_readback_blocks(self):
        self.states["new"]["name"] = "Manual rename"
        result = self.run_section()
        self.assertEqual(result.manifest["section_reason_code"], "NEW_DESTINATION_CHANGED_SINCE_VERIFICATION")

    def test_revoked_section_approval_rejected(self):
        ref = self.approval()
        approval_id = self.journal.read_evidence(ref)["approval_id"]
        self.journal.append("SECTION_APPROVAL_REVOKED", payload={"approval_id": approval_id})
        with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_REVOKED"):
            self.run_section(owner_approval_ref=ref)

    def test_generic_profile_requires_explicit_caller_choice(self):
        self.projection["method_profile"] = "approved_folder_scope"
        with self.assertRaisesRegex(SafetyError, "SECTION_METHOD_PROFILE_NOT_AUTHORIZED"):
            self.run_section()

    def test_generic_independently_approved_folder_move_has_no_fictitious_child_move(self):
        self.projection["method_profile"] = "approved_folder_scope"
        self.states["source"]["mime_type"] = FOLDER
        self.states["source"]["subtree"]["members"] = [{"id": "child"}]
        self.projection["members"].append({"source_id": "child", "direct_operation_refs": [], "ancestor_move_operation_refs": ["MOVE-001/ITEM-001"]})
        self.projection["declared_member_count"] = 2
        result = self.run_section(expected_method_profile="approved_folder_scope")
        self.assertIsNotNone(result.plan)
        self.assertEqual(len(result.plan.operations), 1)
        self.assertEqual(result.manifest["members"][1]["direct_operation_ids"], [])
        self.assertEqual(result.manifest["members"][1]["ancestor_move_operation_ids"], [result.plan.operations[0].operation_id])


class ReuseSectionImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / "state")
        self.export = export_fixture()
        set_cell(self.export, "Proposed Moves", 1, "Decision (Yes/No)", "")
        for pid, item, source_id in [("ROOT", "ROOT-ITEM", "reuse-root"), ("MOVE-002", "ITEM-002", "source2")]:
            append_row(self.export, "Proposed Moves", {"Proposal ID": pid, "Decision (Yes/No)": "", "Action": "Move"})
            append_row(self.export, "Move Manifest", {"Proposal ID": pid, "Manifest item": item, "Source ID": source_id})
        self.states = {"source": metadata("source"), "source2": metadata("source2", "Other"), "existing": metadata("existing", parents=["reuse-root"]),
                       "old": metadata("old", "Old", FOLDER, []), "reuse-root": metadata("reuse-root", "Archive", FOLDER, ["old"]),
                       "target-section": metadata("target-section", "Target", FOLDER, [])}
        self.states["reuse-root"]["subtree"]["members"] = [{"id": "existing", "name": "Minutes", "mime_type": "application/vnd.google-apps.document", "parents": ["reuse-root"]}]
        inventory = {"schema_version": "uqmc.section.source-inventory.v1", "section_id": "fixture-section", "complete": True,
                     "source_container_ids_complete": True, "source_file_ids": ["existing", "source", "source2"],
                     "existing_member_source_ids": ["existing"], "incoming_source_file_ids": ["source", "source2"],
                     "supporting_container_ids": ["reuse-root"], "source_container_ids": ["old", "reuse-root"]}
        inventory_ref = self.journal.evidence(inventory)
        target = {"logical_key": "reuse-archive", "logical_path": "Target/Archive", "folder_name": "Archive", "reused_folder_id": "reuse-root",
                  "original_parent_ids": ["old"], "target_section_id": "target-section", "root_move_operation_ref": "ROOT/ROOT-ITEM"}
        self.warning_ref = self.journal.evidence({"schema_version": "uqmc.section.access-warning.v1", "section_id": "fixture-section",
                                                "target_mode": "REUSE_EXISTING_FOLDER", "affected_source_ids": ["existing"], "shown_to": "fixture-owner"})
        groups = [{"parent_id": "reuse-root", "exact_name": "Minutes", "source_ids": ["source", "existing"]}]
        spec = {"schema_version": "uqmc.section.destination-spec.v1", "section_id": "fixture-section", "model_revision": "reuse-model-v1", "complete": True,
                "target_mode": "REUSE_EXISTING_FOLDER", "reuse_target": target, "collision_policy": "PRESERVE_DISTINCT_IDS", "collision_groups": groups,
                "inherited_access_warning_evidence_ref": self.warning_ref}
        spec_ref = self.journal.evidence(spec)
        self.context_ref = self.journal.evidence({"schema_version": "uqmc.section.review-context.v1", "section_id": "fixture-section", "model_revision": "reuse-model-v1",
            "source_inventory_evidence_ref": inventory_ref, "destination_spec_evidence_ref": spec_ref, "reviewed_by": "fixture-reviewer", "reviewed_at_utc": "2026-01-01T00:00:00Z",
            "provenance": "independently_reviewed_corrected_model"})
        self.projection = {"schema_version": "uqmc.destination.section.v1", "section_id": "fixture-section", "projection_revision": "reuse-projection-v1", "model_revision": "reuse-model-v1",
            "workbook_id": self.export["workbook_id"], "input_sha256": catalog(self.export)["input_sha256"], "complete": True, "method_profile": "existing_folder_consolidation",
            "target_mode": "REUSE_EXISTING_FOLDER", "allowed_actions": ["MOVE"], "allowed_destination_parent_ids": ["reuse-root", "target-section"],
            "declared_member_count": 3, "supporting_container_ids": ["reuse-root"], "source_inventory_evidence_ref": inventory_ref, "reuse_target": target,
            "collision_policy": "PRESERVE_DISTINCT_IDS", "collision_groups": groups,
            "members": [{"source_id": "existing", "direct_operation_refs": [], "ancestor_move_operation_refs": ["ROOT/ROOT-ITEM"]},
                        {"source_id": "source", "direct_operation_refs": ["MOVE-001/ITEM-001"], "ancestor_move_operation_refs": []},
                        {"source_id": "source2", "direct_operation_refs": ["MOVE-002/ITEM-002"], "ancestor_move_operation_refs": []}],
            "operations": [{"reference": "ROOT/ROOT-ITEM", "source_id": "reuse-root", "target_parent_ids": ["target-section"], "depends_on_refs": [], "allowed_collision_ids": []},
                           {"reference": "MOVE-001/ITEM-001", "source_id": "source", "target_parent_ids": ["reuse-root"], "depends_on_refs": ["ROOT/ROOT-ITEM"], "target_logical_key": "reuse-archive", "allowed_collision_ids": ["existing"]},
                           {"reference": "MOVE-002/ITEM-002", "source_id": "source2", "target_parent_ids": ["reuse-root"], "depends_on_refs": ["ROOT/ROOT-ITEM"], "target_logical_key": "reuse-archive", "allowed_collision_ids": []}]}

    def tearDown(self):
        self.temp.cleanup()

    def approval(self, **overrides):
        value = {"schema_version": "uqmc.section.owner-approval.v1", "decision": "APPROVED", "actor": "fixture-owner", "approval_id": "reuse-approval",
            "approved_at_utc": "2026-01-01T00:00:00Z", "provenance": {"kind": "explicit_owner_instruction", "evidence_reference": "fixture-message"},
            "section_id": "fixture-section", "method_profile": "existing_folder_consolidation", "target_mode": "REUSE_EXISTING_FOLDER", "supporting_container_ids": ["reuse-root"],
            "projection_revision": self.projection["projection_revision"], "projection_sha256": digest(self.projection), "allowed_actions": ["MOVE"],
            "member_source_ids": [m["source_id"] for m in self.projection["members"]], "operation_refs": [op["reference"] for op in self.projection["operations"]],
            "allowed_destination_parent_ids": self.projection["allowed_destination_parent_ids"],
            "inherited_access_acknowledgement": {"accepted": True, "warning_evidence_ref": self.warning_ref, "affected_source_ids": ["existing"]}}
        value.update(overrides)
        return self.journal.evidence(value)

    def run_import(self, **overrides):
        resolved = resolutions(self.export, self.states)
        for operation in self.projection["operations"]:
            resolved[operation["reference"]].update(target_parent_ids=operation["target_parent_ids"], depends_on_refs=operation["depends_on_refs"], allowed_collision_ids=operation["allowed_collision_ids"])
        args = {"projection": self.projection, "owner_approval_ref": self.approval(), "expected_section_id": "fixture-section", "batch_id": "reuse-batch",
                "metadata_by_id": self.states, "resolutions": resolved, "review_context_ref": self.context_ref, "expected_method_profile": "existing_folder_consolidation"}
        args.update(overrides)
        return import_section(self.journal, self.export, **args)

    def test_reuse_has_one_root_plus_exact_incoming_operations(self):
        result = self.run_import()
        self.assertIsNotNone(result.plan)
        self.assertEqual(len(result.plan.operations), 3)
        self.assertEqual({op.file_id for op in result.plan.operations}, {"reuse-root", "source", "source2"})
        self.assertEqual(result.manifest["members"][0]["direct_operation_ids"], [])
        self.assertEqual(self.states["existing"]["parents"], ["reuse-root"])
        self.assertEqual(next(op for op in result.plan.operations if op.file_id == "source").allowed_collision_ids, ("existing",))
        self.assertFalse(result.manifest["execution_approved"])

    def test_reuse_mode_requires_explicit_owner_mode(self):
        with self.assertRaisesRegex(SafetyError, "SECTION_REUSE_APPROVAL_SCOPE_MISMATCH"):
            self.run_import(owner_approval_ref=self.approval(target_mode="NEW_FOLDER_REQUIRED"))

    def test_reuse_independent_target_cannot_be_replaced(self):
        self.projection["reuse_target"] = {**self.projection["reuse_target"], "target_section_id": "wrong-section"}
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "INDEPENDENT_REUSE_TARGET_BINDING_MISMATCH")

    def test_reuse_root_old_parents_are_exact(self):
        self.states["reuse-root"]["parents"] = ["manual-parent"]
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "REUSE_ROOT_PREIMAGE_MISMATCH")

    def test_reuse_cannot_substitute_incoming_folder(self):
        self.states["source2"]["mime_type"] = FOLDER
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "REUSE_INCOMING_FILE_PREIMAGE_MISMATCH")

    def test_reuse_existing_file_set_rejects_folder_identity(self):
        self.states["existing"]["mime_type"] = FOLDER
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "REUSE_EXISTING_MEMBER_NOT_FILE_UNDER_ROOT")

    def test_reuse_preserves_existing_children_no_direct_moves(self):
        self.projection["members"][0]["ancestor_move_operation_refs"] = []
        with self.assertRaisesRegex(SafetyError, "SECTION_MEMBER_UNMAPPED"):
            self.run_import()

    def test_reuse_requires_historical_access_warning_acknowledgement(self):
        result = self.run_import(owner_approval_ref=self.approval(inherited_access_acknowledgement={"accepted": False}))
        self.assertEqual(result.manifest["section_reason_code"], "REUSE_INHERITED_ACCESS_ACKNOWLEDGEMENT_REQUIRED")

    def test_reuse_collision_ids_cannot_expand(self):
        self.projection["operations"][1]["allowed_collision_ids"].append("source2")
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "REUSE_COLLISION_ALLOWLIST_MISMATCH")

    def test_reuse_missing_root_dependency_rejected(self):
        self.projection["operations"][1]["depends_on_refs"] = []
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "INCOMING_TARGET_OR_DEPENDENCY_MISMATCH")

    def test_reuse_no_decision_remains_excluded(self):
        set_cell(self.export, "Proposed Moves", 2, "Decision (Yes/No)", "No")
        self.projection["input_sha256"] = catalog(self.export)["input_sha256"]
        result = self.run_import(metadata_by_id=GuardedMetadata())
        self.assertEqual(result.manifest["section_reason_code"], "SECTION_CONTAINS_EXCLUDED_MOVE_RECORD")


class ReuseRenameSectionTests(ReuseSectionImporterTests):
    def setUp(self):
        super().setUp()
        # Each inherited/incoming file and the supporting root has an exact
        # independent workbook rename binding; no row decision is fabricated.
        set_cell(self.export, "Proposed Renames", 1, "Decision (Yes/No)", "")
        names = {"source": ("RENAME-001", "Source renamed"), "source2": ("RENAME-002", "Other renamed"),
                 "existing": ("RENAME-EXISTING", "Historical renamed"), "reuse-root": ("RENAME-ROOT", "Renamed archive")}
        for source_id, (ref, title) in names.items():
            if source_id != "source":
                append_row(self.export, "Proposed Renames", {"Rename ID": ref, "Source ID": source_id, "Decision (Yes/No)": "",
                    "Current name + type suffix": self.states[source_id]["name"], "Proposed name + type suffix": title})
            else:
                set_cell(self.export, "Proposed Renames", 1, "Proposed name + type suffix", title)
        source = catalog(self.export)
        self.projection["input_sha256"] = source["input_sha256"]
        self.projection["allowed_actions"] = ["MOVE", "RENAME"]
        self.projection["reuse_target"]["final_folder_name"] = "Renamed archive"
        self.projection["collision_groups"] = []
        rename_ops, bindings = [], []
        for source_id in ("reuse-root", "existing", "source", "source2"):
            ref, title = names[source_id]
            rename_ops.append({"reference": ref, "source_id": source_id, "action": "RENAME", "exact_target_name": title,
                               "depends_on_refs": [] if source_id == "reuse-root" else ["RENAME-ROOT"], "allowed_collision_ids": []})
            bindings.append({"reference": ref, "source_id": source_id, "exact_current_name": self.states[source_id]["name"],
                             "exact_target_name": title, "workbook_row_sha256": source["records"][ref]["row_sha256"]})
        self.projection["rename_bindings"] = bindings
        for operation in self.projection["operations"]:
            operation["allowed_collision_ids"] = []
            if operation["source_id"] == "reuse-root":
                operation["depends_on_refs"] = ["RENAME-ROOT", "RENAME-EXISTING", "RENAME-001", "RENAME-002"]
            else:
                operation["depends_on_refs"].append(names[operation["source_id"]][0])
        self.projection["operations"] = rename_ops + self.projection["operations"]
        for member in self.projection["members"]:
            member["direct_operation_refs"].insert(0, names[member["source_id"]][0])
        context = self.journal.read_evidence(self.context_ref)
        spec = self.journal.read_evidence(context["destination_spec_evidence_ref"])
        spec["reuse_target"] = copy.deepcopy(self.projection["reuse_target"])
        spec["collision_groups"] = []
        spec["rename_bindings"] = bindings
        spec["rename_collision_review_evidence_ref"] = self.journal.evidence({"schema_version": "uqmc.section.rename-collision-review.v1", "section_id": "fixture-section",
            "model_revision": "reuse-model-v1", "complete": True, "rename_bindings_sha256": digest(bindings), "collisions": []})
        context["destination_spec_evidence_ref"] = self.journal.evidence(spec)
        self.context_ref = self.journal.evidence(context)

    def approval(self, **overrides):
        ref = super().approval(**overrides)
        value = self.journal.read_evidence(ref)
        value["allowed_actions"] = self.projection["allowed_actions"]
        return self.journal.evidence(value)

    def refresh_rename_review(self):
        source = catalog(self.export)
        self.projection["input_sha256"] = source["input_sha256"]
        for binding in self.projection["rename_bindings"]:
            binding["workbook_row_sha256"] = source["records"][binding["reference"]]["row_sha256"]
        context = self.journal.read_evidence(self.context_ref)
        spec = self.journal.read_evidence(context["destination_spec_evidence_ref"])
        spec["rename_bindings"] = copy.deepcopy(self.projection["rename_bindings"])
        spec["reuse_target"] = copy.deepcopy(self.projection["reuse_target"])
        review = self.journal.read_evidence(spec["rename_collision_review_evidence_ref"])
        review["rename_bindings_sha256"] = digest(spec["rename_bindings"])
        spec["rename_collision_review_evidence_ref"] = self.journal.evidence(review)
        context["destination_spec_evidence_ref"] = self.journal.evidence(spec)
        self.context_ref = self.journal.evidence(context)

    def run_import(self, **overrides):
        resolved = resolutions(self.export, self.states)
        for operation in self.projection["operations"]:
            row = resolved[operation["reference"]]
            row.update(depends_on_refs=operation["depends_on_refs"], allowed_collision_ids=operation["allowed_collision_ids"])
            if operation.get("action") == "RENAME":
                row["exact_target_name"] = operation["exact_target_name"]
            else:
                row["target_parent_ids"] = operation["target_parent_ids"]
        args = {"projection": self.projection, "owner_approval_ref": self.approval(), "expected_section_id": "fixture-section", "batch_id": "reuse-rename-batch",
                "metadata_by_id": self.states, "resolutions": resolved, "review_context_ref": self.context_ref, "expected_method_profile": "existing_folder_consolidation"}
        args.update(overrides)
        return import_section(self.journal, self.export, **args)

    def test_rename_then_move_exact_scope_and_ancestor_baseline(self):
        result = self.run_import()
        self.assertIsNotNone(result.plan)
        self.assertEqual(sum(op.action == "RENAME" for op in result.plan.operations), 4)
        self.assertEqual(sum(op.action == "MOVE" for op in result.plan.operations), 3)
        root_move = next(op for op in result.plan.operations if op.file_id == "reuse-root" and op.action == "MOVE")
        self.assertEqual(root_move.before["name"], "Renamed archive")
        self.assertEqual(root_move.before["subtree"]["members"][0]["name"], "Historical renamed")

    def test_move_cannot_precede_own_rename(self):
        move = next(op for op in self.projection["operations"] if op["reference"] == "MOVE-001/ITEM-001")
        move["depends_on_refs"].remove("RENAME-001")
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "INCOMING_MOVE_MUST_WAIT_FOR_OWN_RENAME")

    def test_root_move_waits_carried_file_rename(self):
        move = next(op for op in self.projection["operations"] if op["reference"] == "ROOT/ROOT-ITEM")
        move["depends_on_refs"].remove("RENAME-EXISTING")
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "ROOT_MOVE_MUST_WAIT_FOR_ALL_SECTION_RENAMES")

    def test_root_move_waits_incoming_file_rename_even_before_its_move(self):
        move = next(op for op in self.projection["operations"] if op["reference"] == "ROOT/ROOT-ITEM")
        move["depends_on_refs"].remove("RENAME-002")
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "ROOT_MOVE_MUST_WAIT_FOR_ALL_SECTION_RENAMES")

    def test_modified_workbook_rename_requires_independent_revalidation(self):
        self.projection["rename_bindings"][0]["exact_target_name"] = "Unauthorized"
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "INDEPENDENT_RENAME_BINDING_MISMATCH")

    def test_consistent_wrong_binding_and_resolution_cannot_override_workbook(self):
        binding = next(item for item in self.projection["rename_bindings"] if item["source_id"] == "source")
        operation = next(item for item in self.projection["operations"] if item["reference"] == binding["reference"])
        binding["exact_target_name"] = operation["exact_target_name"] = "Unrelated approved-looking title"
        self.refresh_rename_review()
        result = self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["section_reason_code"], "WORKBOOK_RENAME_TARGET_MISMATCH")

    def test_folder_binding_cannot_override_workbook(self):
        binding = next(item for item in self.projection["rename_bindings"] if item["source_id"] == "reuse-root")
        operation = next(item for item in self.projection["operations"] if item["reference"] == binding["reference"])
        binding["exact_target_name"] = operation["exact_target_name"] = "Wrong root title"
        self.projection["reuse_target"]["final_folder_name"] = "Wrong root title"
        self.refresh_rename_review()
        result = self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["section_reason_code"], "WORKBOOK_RENAME_TARGET_MISMATCH")

    def test_native_display_suffix_requires_explicit_conversion_evidence(self):
        set_cell(self.export, "Proposed Renames", 1, "Proposed name + type suffix", "Source renamed.gdoc")
        self.refresh_rename_review()
        result = self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["section_reason_code"], "VERIFIED_NATIVE_DISPLAY_CONVERSION_REQUIRED")

    def test_native_display_suffix_conversion_is_exact_and_mime_bound(self):
        set_cell(self.export, "Proposed Renames", 1, "Proposed name + type suffix", "Source renamed.gdoc")
        self.refresh_rename_review()
        binding = next(item for item in self.projection["rename_bindings"] if item["source_id"] == "source")
        provenance_ref = self.journal.evidence({"kind": "fixture_display_title_review"})
        binding["native_display_conversion_ref"] = self.journal.evidence({
            "schema_version": "uqmc.rename.native-display-conversion.v1", "source_id": "source",
            "mime_type": "application/vnd.google-apps.document", "workbook_row_sha256": binding["workbook_row_sha256"],
            "workbook_proposed_name": "Source renamed.gdoc", "exact_target_name": "Source renamed",
            "display_suffix": ".gdoc", "provenance_ref": provenance_ref})
        self.refresh_rename_review()
        result = self.run_import()
        self.assertIsNotNone(result.plan)
        self.assertEqual(next(op.target["name"] for op in result.plan.operations if op.file_id == "source" and op.action == "RENAME"), "Source renamed")

    def test_wrong_native_mime_suffix_is_not_normalized(self):
        set_cell(self.export, "Proposed Renames", 1, "Proposed name + type suffix", "Source renamed.gsheet")
        self.refresh_rename_review()
        result = self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["section_reason_code"], "WORKBOOK_RENAME_TARGET_MISMATCH")

    def test_uploaded_file_binding_cannot_override_workbook(self):
        self.states["source"]["mime_type"] = "application/pdf"
        self.states["source"]["name"] = "Minutes.pdf"
        set_cell(self.export, "Proposed Renames", 1, "Proposed name + type suffix", "Source renamed.pdf")
        binding = next(item for item in self.projection["rename_bindings"] if item["source_id"] == "source")
        operation = next(item for item in self.projection["operations"] if item["reference"] == binding["reference"])
        binding["exact_current_name"] = "Minutes.pdf"
        binding["exact_target_name"] = operation["exact_target_name"] = "Other title.pdf"
        self.refresh_rename_review()
        result = self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest["section_reason_code"], "WORKBOOK_RENAME_TARGET_MISMATCH")

    def test_source_parent_rename_collision_needs_reviewed_strategy(self):
        context = self.journal.read_evidence(self.context_ref)
        spec = self.journal.read_evidence(context["destination_spec_evidence_ref"])
        review = self.journal.read_evidence(spec["rename_collision_review_evidence_ref"])
        review["collisions"] = [{"source_id": "source", "occupied_by": "source2"}]
        spec["rename_collision_review_evidence_ref"] = self.journal.evidence(review)
        context["destination_spec_evidence_ref"] = self.journal.evidence(spec)
        result = self.run_import(review_context_ref=self.journal.evidence(context))
        self.assertEqual(result.manifest["section_reason_code"], "RENAME_COLLISION_REQUIRES_REVIEWED_ORDER_OR_STRATEGY")

    # These base cases specifically assert the move-only shape and are covered
    # by ReuseSectionImporterTests; rename-profile cases use different counts.
    def test_reuse_has_one_root_plus_exact_incoming_operations(self):
        self.test_rename_then_move_exact_scope_and_ancestor_baseline()

    def test_reuse_collision_ids_cannot_expand(self):
        operation = next(op for op in self.projection["operations"] if op["reference"] == "MOVE-001/ITEM-001")
        operation["allowed_collision_ids"].append("source2")
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "REUSE_COLLISION_ALLOWLIST_MISMATCH")

    def test_reuse_missing_root_dependency_rejected(self):
        operation = next(op for op in self.projection["operations"] if op["reference"] == "MOVE-001/ITEM-001")
        operation["depends_on_refs"].remove("ROOT/ROOT-ITEM")
        result = self.run_import()
        self.assertEqual(result.manifest["section_reason_code"], "INCOMING_TARGET_OR_DEPENDENCY_MISMATCH")

    def test_reuse_preserves_existing_children_no_direct_moves(self):
        result = self.run_import()
        existing_ops = [op for op in result.plan.operations if op.file_id == "existing"]
        self.assertEqual([op.action for op in existing_ops], ["RENAME"])


if __name__ == "__main__":
    unittest.main()
