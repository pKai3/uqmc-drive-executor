"""Synthetic section-import -> agent-profile registration integration."""
import copy
import unittest
from datetime import datetime, timedelta, timezone

import test_section_importer as section_fixtures
from test_importer import resolutions
from uqmc_recovery.action_bridge import AGENT_LIMITATIONS, ActionBridge
from uqmc_recovery.agent_profile import agent_snapshot, register_agent_plan, validate_agent_plan
from uqmc_recovery.model import Approval, FOLDER, SafetyError, digest
from uqmc_recovery.section_importer import validate_section_authority


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

    def _enabled_section_handoff(self):
        """Import actual fixture rows, then bind fixture authority and review."""
        self.partial["reuse-root"]["subtree"]["membership_scope"] = "DESCENDANTS"
        self.partial["old"]["subtree"].update(membership_scope="DIRECT_CHILDREN",
            members=[{"id": key} for key in ("reuse-root", "source", "source2")])
        self.partial["target-section"]["subtree"]["membership_scope"] = "DIRECT_CHILDREN"
        for operation in self.fixture.projection["operations"]:
            self.resolved[operation["reference"]]["live_snapshot_sha256"] = digest(
                agent_snapshot(self.partial[operation["source_id"]]))
        imported = self.fixture.run_import(metadata_profile="AGENT_ASSISTED",
            metadata_by_id=self.partial, resolutions=self.resolved)
        self.assertIsNotNone(imported.plan, imported.manifest)
        self.plan = imported.plan
        self.journal = self.fixture.journal
        register_agent_plan(self.journal, self.plan)
        self.root_move = next(op for op in self.plan.operations if op.file_id == "reuse-root")
        self.assertFalse(self.root_move.depends_on)
        self.before = self.root_move.before
        self.controller_id = "synthetic-section-controller"
        self.bridge = ActionBridge(self.journal, "AGENT_ASSISTED", controller_id=self.controller_id)
        clock = datetime.now(timezone.utc)
        self.approval = Approval("synthetic-batch-approval", self.plan.run_id, self.plan.batch_id,
            self.plan.proposal_revision, self.plan.proposal_hash,
            tuple(op.operation_id for op in self.plan.operations), "fixture-owner",
            (clock - timedelta(minutes=1)).isoformat(), (clock + timedelta(hours=1)).isoformat(), "AGENT_ASSISTED")
        self.section_approval_id = imported.manifest["authorization_scope"]["owner_approval_id"]
        required_ids = {op.file_id for op in self.plan.operations}
        for op in self.plan.operations:
            required_ids |= set(op.before["parents"]) | set(op.target.get("parents", []))
        auth = {"kind": "EXPLICIT_BATCH_AUTHORIZATION", "authority": "human_user", "authorized": True,
            "actor": self.approval.actor, "approval_hash": digest(self.approval.body()),
            "proposal_hash": self.plan.proposal_hash,
            "section_authority": validate_section_authority(self.journal, self.plan),
            "provenance_ref": self.journal.evidence({"kind": "TRUSTED_USER_INSTRUCTION", "text": "Synthetic section approval only."}),
            "limitations_acknowledged": sorted(AGENT_LIMITATIONS), "access_policy": "SCOPED_EXPOSURE_ACCEPTED",
            "exposure_review_ref": self.journal.evidence({"kind": "SCOPED_EXPOSURE_REVIEW", "proposal_hash": self.plan.proposal_hash,
                "operation_ids": list(self.approval.operation_ids), "warning_presented": True, "approved": True,
                "newly_exposed_principals": [], "unknown_exposure": True,
                "access_baselines": {key: self.journal.evidence(agent_snapshot(self.partial[key])) for key in required_ids}})}
        enable = {"kind": "DISPATCH_ENABLE_REVIEW", "controller_id": self.controller_id,
            "proposal_hash": self.plan.proposal_hash, "approval_id": self.approval.approval_id,
            "authorization_hash": digest(auth), "manifest_verified": True, "reviewer": "synthetic-reviewer",
            "reviewed_at": clock.isoformat(), "expires_at": (clock + timedelta(minutes=30)).isoformat(),
            "provenance_ref": self.journal.evidence({"kind": "TRUSTED_CONTROLLER_REVIEW", "test_only": True})}
        enabled = self.bridge.authorize_agent_dispatch(self.plan, self.approval, auth, enable)
        self.assertTrue(enabled["agent_handoff_enabled"])
        self.assertEqual(enabled["external_tool_calls"], 0)
        self.assertNotEqual(self.section_approval_id, self.approval.approval_id)
        return imported

    def _section_preflight(self):
        """Fresh synthetic receipts; the bridge never calls an external tool."""
        def read_proof(value):
            raw = {key: copy.deepcopy(value[key]) for key in
                ("id", "name", "mime_type", "parents", "version", "permissions") if key in value}
            return {"kind": "AGENT_CONNECTOR_READ", "observed_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_ref": self.journal.evidence(value),
                "receipt_ref": self.journal.evidence({"structuredContent": raw})}
        intended = self.root_move.intended(self.before)
        parents = {key: agent_snapshot(self.partial[key]) for key in ("old", "target-section")}
        return {"complete": True, "no_cycle": True, "parents": parents,
            "parent_reads": {key: read_proof(value) for key, value in parents.items()},
            "source_read": read_proof(self.before), "collision_ids": [],
            "collision_review_ref": self.journal.evidence({"kind": "COLLISION_REVIEW", "complete": True,
                "proposal_hash": self.plan.proposal_hash, "operation_id": self.root_move.operation_id,
                "target_name": intended["name"], "target_parent_ids": intended["parents"], "collision_ids": [],
                "observed_at": datetime.now(timezone.utc).isoformat()}),
            "membership_ref": self.journal.evidence({"kind": "COMPLETE_MEMBERSHIP_READ", "root_id": self.before["id"],
                "complete": True, "pagination_complete": True, "membership_scope": "DESCENDANTS",
                "member_ids": [member["id"] for member in self.before["subtree"]["members"]],
                "observed_at": datetime.now(timezone.utc).isoformat()}),
            "expected_after": intended, "ancestor_ids": [],
            "concurrency": {"mechanism": "SINGLE_CONTROLLER_FRESH_READS", "controller_id": self.controller_id,
                "atomic_precondition": False, "race_window_acknowledged": True}}

    def _prepare_section_root(self):
        return self.bridge.prepare_action(self.plan, self.root_move.operation_id,
            self.approval, self.before, self._section_preflight())

    def _cancel_section(self, kind):
        self.journal.append(kind, payload={"approval_id": self.section_approval_id})
        # The batch approval and controller enable remain present. The origin
        # section authority itself must therefore stop either dispatch gate.
        self.assertFalse(any(event["kind"] == "REVOKED" and
            event["payload"].get("approval_id") == self.approval.approval_id for event in self.journal.records()))

    def test_section_import_enable_and_prepare_accepts_current_authority(self):
        self._enabled_section_handoff()
        envelope = self._prepare_section_root()
        self.assertEqual(envelope["file_id"], "reuse-root")
        self.assertEqual(self.journal.records()[-1]["kind"], "PREPARED")
        self.assertFalse(any(event["kind"] == "ATTEMPTED" for event in self.journal.records()))

    def test_section_revoked_after_enable_blocks_prepare(self):
        self._enabled_section_handoff()
        self._cancel_section("SECTION_APPROVAL_REVOKED")
        with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_REVOKED_OR_SUPERSEDED"):
            self._prepare_section_root()
        self.assertFalse(any(event["kind"] in {"PREPARED", "ATTEMPTED"} for event in self.journal.records()))

    def test_section_superseded_after_enable_blocks_prepare(self):
        self._enabled_section_handoff()
        self._cancel_section("SECTION_APPROVAL_SUPERSEDED")
        with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_REVOKED_OR_SUPERSEDED"):
            self._prepare_section_root()
        self.assertFalse(any(event["kind"] in {"PREPARED", "ATTEMPTED"} for event in self.journal.records()))

    def test_section_revoked_after_prepare_blocks_attempt(self):
        self._enabled_section_handoff()
        envelope = self._prepare_section_root()
        self._cancel_section("SECTION_APPROVAL_REVOKED")
        with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_REVOKED_OR_SUPERSEDED"):
            self.bridge.mark_attempt(envelope["envelope_hash"], self.before, self._section_preflight())
        self.assertFalse(any(event["kind"] == "ATTEMPTED" for event in self.journal.records()))

    def test_section_superseded_after_prepare_blocks_attempt(self):
        self._enabled_section_handoff()
        envelope = self._prepare_section_root()
        self._cancel_section("SECTION_APPROVAL_SUPERSEDED")
        with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_REVOKED_OR_SUPERSEDED"):
            self.bridge.mark_attempt(envelope["envelope_hash"], self.before, self._section_preflight())
        self.assertFalse(any(event["kind"] == "ATTEMPTED" for event in self.journal.records()))


if __name__ == "__main__":
    unittest.main()
