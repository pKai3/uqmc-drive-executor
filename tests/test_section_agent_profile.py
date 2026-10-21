"""Synthetic section-import -> agent-profile registration integration."""
import copy
import unittest

import test_section_importer as section_fixtures
from test_importer import resolutions
from uqmc_recovery.agent_profile import agent_snapshot, register_agent_plan, validate_agent_plan
from uqmc_recovery.model import FOLDER, digest


class SectionAgentProfileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = section_fixtures.ReuseSectionImporterTests()
        self.fixture.setUp()
        self.partial = copy.deepcopy(self.fixture.states)
        for source in self.partial.values():
            source["parents_complete"] = True
            for key in ("version", "modified_time", "trashed", "permissions", "owners", "drive_id", "capabilities", "access"):
                source.pop(key, None)
            if source["mime_type"] != FOLDER:
                source.pop("subtree", None)
        # Preserve a genuinely returned null and a genuinely available grant list.
        self.partial["source"]["version"] = None
        self.partial["source"]["permissions"] = [{"id": "fixture-grant", "type": "user", "role": "reader"}]
        self.resolved = resolutions(self.fixture.export, self.partial)
        for operation in self.fixture.projection["operations"]:
            self.resolved[operation["reference"]].update(
                target_parent_ids=operation["target_parent_ids"], depends_on_refs=operation["depends_on_refs"],
                allowed_collision_ids=operation["allowed_collision_ids"],
                live_snapshot_sha256=digest(agent_snapshot(self.partial[operation["source_id"]])))

    def tearDown(self):
        self.fixture.tearDown()

    def test_partial_section_import_can_be_sealed_without_fabricated_metadata(self):
        imported = self.fixture.run_import(metadata_profile="AGENT_ASSISTED", metadata_by_id=self.partial, resolutions=self.resolved)
        self.assertIsNotNone(imported.plan, imported.manifest)
        self.assertEqual(imported.manifest["metadata_profile"], "AGENT_ASSISTED")
        validate_agent_plan(imported.plan)
        registered = register_agent_plan(self.fixture.journal, imported.plan)
        self.assertEqual(registered["payload"]["profile"], "AGENT_ASSISTED")
        sealed = self.fixture.journal.read_evidence(registered["payload"]["plan_ref"])
        source = next(op["before"] for op in sealed["operations"] if op["file_id"] == "source")
        self.assertIsNone(source["version"])
        self.assertEqual(source["permissions"], self.partial["source"]["permissions"])
        self.assertNotIn("capabilities", source)
        self.assertNotIn("access", source)
        self.assertNotIn("trashed", source)
        self.assertIn("effective_access", source["metadata_unavailable"])
        self.assertEqual(source["parents"], ["old"])
        self.assertFalse(imported.manifest["execution_approved"])
        self.assertFalse(any(event["kind"] in {"APPROVED", "PREPARED", "ATTEMPTED"} for event in self.fixture.journal.records()))

    def test_same_partial_observation_is_blocked_by_strict_profile(self):
        imported = self.fixture.run_import(metadata_profile="STRICT", metadata_by_id=self.partial, resolutions=self.resolved)
        self.assertIsNone(imported.plan)
        self.assertEqual(imported.manifest["section_reason_code"], "INCOMPLETE_METADATA")

    def test_partial_profile_still_requires_exact_complete_parents(self):
        self.partial["reuse-root"].pop("parents_complete")
        imported = self.fixture.run_import(metadata_profile="AGENT_ASSISTED", metadata_by_id=self.partial, resolutions=self.resolved)
        self.assertIsNone(imported.plan)
        self.assertEqual(imported.manifest["section_reason_code"], "AGENT_COMPLETE_PARENT_IDS_REQUIRED")


if __name__ == "__main__":
    unittest.main()
