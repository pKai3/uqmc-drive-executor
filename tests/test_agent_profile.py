import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from uqmc_recovery.agent_profile import (agent_semantic, agent_snapshot,
    register_agent_plan, validate_agent_operation, validate_agent_plan)
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, Operation, Plan, SafetyError, snapshot


def observed(file_id="file", name="Notes", mime="application/vnd.google-apps.document", parents=None):
    result = {"id": file_id, "name": name, "mime_type": mime, "parents": ["old"] if parents is None else parents, "parents_complete": True}
    if mime == FOLDER:
        result["subtree"] = {"complete": True, "members": []}
    return result


def candidate():
    return Plan("batch", "batch", "revision", (Operation("move-file", "file", "MOVE", observed(), {"parents": ["new"]}, proposal_refs=("MOVE-001",)),))


class AgentProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name) / "state")

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_optional_metadata_is_honest(self):
        source = observed()
        result = agent_snapshot(source)
        self.assertNotIn("version", result)
        self.assertNotIn("permissions", result)
        self.assertNotIn("capabilities", result)
        self.assertNotIn("trashed", result)
        self.assertIn("version", result["metadata_unavailable"])
        self.assertIn("effective_access", result["metadata_unavailable"])
        self.assertEqual(source, observed())

    def test_null_stays_null_and_known_grants_survive(self):
        source = observed()
        source.update(version=None, permissions=[{"type": "user", "role": "reader", "id": "grant"}])
        result = agent_snapshot(source)
        self.assertIsNone(result["version"])
        self.assertEqual(result["permissions"], source["permissions"])
        self.assertNotIn("permissions", result["metadata_unavailable"])

    def test_nonfolder_null_subtree_is_not_fabricated_or_crashes(self):
        source = observed()
        source["subtree"] = None
        plan = replace(candidate(), operations=(replace(candidate().operations[0], before=source),))
        self.assertIsNone(agent_snapshot(source)["subtree"])
        validate_agent_plan(plan)
        register_agent_plan(self.journal, plan)

    def test_strict_validator_still_rejects_partial_metadata(self):
        with self.assertRaisesRegex(SafetyError, "INCOMPLETE_METADATA"):
            snapshot(observed())
        validate_agent_plan(candidate())

    def test_complete_parent_ids_required(self):
        source = observed()
        del source["parents_complete"]
        with self.assertRaisesRegex(SafetyError, "AGENT_COMPLETE_PARENT_IDS_REQUIRED"):
            agent_snapshot(source)

    def test_trashed_known_true_rejected(self):
        source = observed()
        source["trashed"] = True
        with self.assertRaisesRegex(SafetyError, "TRASHED_SOURCE"):
            agent_snapshot(source)

    def test_url_or_path_identity_rejected(self):
        source = observed("https://example.test/file")
        with self.assertRaisesRegex(SafetyError, "AGENT_EXACT_ID"):
            agent_snapshot(source)

    def test_folder_requires_complete_descendant_scope(self):
        source = observed("folder", "Folder", FOLDER)
        source["subtree"]["complete"] = False
        with self.assertRaisesRegex(SafetyError, "INCOMPLETE_DESCENDANTS"):
            agent_snapshot(source)

    def test_duplicate_descendant_ids_rejected(self):
        source = observed("folder", "Folder", FOLDER)
        source["subtree"]["members"] = [{"id": "child"}, {"id": "child"}]
        with self.assertRaisesRegex(SafetyError, "INVALID_DESCENDANT_SCOPE"):
            agent_snapshot(source)

    def test_semantic_comparison_ignores_observation_time_but_retains_permissions(self):
        first = observed()
        second = copy.deepcopy(first)
        second.update(version="2", observed_at_utc="2026-10-21T00:00:00Z", modified_time="later", missing_fields=["x"])
        self.assertEqual(agent_semantic(first), agent_semantic(second))
        second["permissions"] = []
        self.assertNotEqual(agent_semantic(first), agent_semantic(second))

    def test_native_suffix_cannot_be_added(self):
        operation = replace(candidate().operations[0], action="RENAME", target={"name": "Notes.gdoc"})
        with self.assertRaisesRegex(SafetyError, "SYNTHETIC_NATIVE_EXTENSION"):
            validate_agent_operation(operation)

    def test_uploaded_extension_retained(self):
        operation = replace(candidate().operations[0], before=observed(name="Report.pdf", mime="application/pdf"), action="RENAME", target={"name": "Report"})
        with self.assertRaisesRegex(SafetyError, "UPLOADED_EXTENSION_CHANGED"):
            validate_agent_operation(operation)

    def test_dotted_filename_stem_can_change_while_pdf_extension_stays(self):
        operation = replace(candidate().operations[0], before=observed(name="Certificate.2024.pdf", mime="application/pdf"),
                            action="RENAME", target={"name": "2024Certificate.pdf"})
        validate_agent_operation(operation)
        with self.assertRaisesRegex(SafetyError, "UPLOADED_EXTENSION_CHANGED"):
            validate_agent_operation(replace(operation, target={"name": "2024Certificate.docx"}))

    def test_compound_archive_extension_is_preserved(self):
        operation = replace(candidate().operations[0], before=observed(name="Archive.2024.tar.gz", mime="application/gzip"),
                            action="RENAME", target={"name": "2024Archive.tar.gz"})
        validate_agent_operation(operation)
        with self.assertRaisesRegex(SafetyError, "UPLOADED_EXTENSION_CHANGED"):
            validate_agent_operation(replace(operation, target={"name": "2024Archive.gz"}))

    def test_only_metadata_scoped_actions(self):
        operation = replace(candidate().operations[0], target={"parents": ["new"], "name": "Other"})
        with self.assertRaisesRegex(SafetyError, "FIELD_SCOPE"):
            validate_agent_operation(operation)

    def test_dependency_and_overlap_rules_retained(self):
        op1 = candidate().operations[0]
        op2 = replace(op1, operation_id="rename-file", action="RENAME", target={"name": "New"})
        with self.assertRaisesRegex(SafetyError, "UNORDERED_SAME_FILE"):
            validate_agent_plan(replace(candidate(), operations=(op1, op2)))
        validate_agent_plan(replace(candidate(), operations=(op1, replace(op2, depends_on=(op1.operation_id,)))))

    def test_collision_ids_are_explicit_and_cannot_be_self(self):
        operation = replace(candidate().operations[0], allowed_collision_ids=("other",))
        validate_agent_operation(operation)
        with self.assertRaisesRegex(SafetyError, "INVALID_COLLISION_SCOPE"):
            validate_agent_operation(replace(operation, allowed_collision_ids=("file",)))

    def test_register_is_idempotent_and_never_approves(self):
        first = register_agent_plan(self.journal, candidate())
        second = register_agent_plan(self.journal, candidate())
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["payload"]["profile"], "AGENT_ASSISTED")
        self.assertFalse(first["payload"]["execution_authorized"])
        self.assertEqual([r["kind"] for r in self.journal.records()], ["DRAFTED"])

    def test_register_rejects_changed_frozen_batch(self):
        register_agent_plan(self.journal, candidate())
        changed = replace(candidate(), proposal_revision="revision2")
        with self.assertRaisesRegex(SafetyError, "BATCH_ALREADY_FROZEN"):
            register_agent_plan(self.journal, changed)

    def test_register_rejects_reused_operation_in_new_batch(self):
        register_agent_plan(self.journal, candidate())
        changed = replace(candidate(), run_id="batch2", batch_id="batch2")
        with self.assertRaisesRegex(SafetyError, "OPERATION_ID_REUSED"):
            register_agent_plan(self.journal, changed)

    def test_register_enforces_durable_file_and_record_exclusions(self):
        self.journal.append("EXCLUDED", payload={"file_id": "file"})
        with self.assertRaisesRegex(SafetyError, "EXCLUDED"):
            register_agent_plan(self.journal, candidate())

    def test_register_enforces_durable_proposal_exclusion(self):
        self.journal.append("PROPOSAL_EXCLUDED", payload={"proposal_ref": "MOVE-001"})
        with self.assertRaisesRegex(SafetyError, "PROPOSAL_EXCLUDED"):
            register_agent_plan(self.journal, candidate())

    def test_register_fences_uncertain_prior_attempt(self):
        self.journal.append("ATTEMPTED", operation_id="other-op", payload={"file_id": "file"})
        with self.assertRaisesRegex(SafetyError, "UNRESOLVED_PRIOR_REQUEST"):
            register_agent_plan(self.journal, candidate())


if __name__ == "__main__":
    unittest.main()
