"""Focused direct-loop tests: temporary journals, fake API, no credentials/network."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from uqmc_recovery.action_bridge import ActionBridge, AGENT_LIMITATIONS
from uqmc_recovery.agent_profile import agent_snapshot, register_agent_plan
from uqmc_recovery.direct import DirectRunner, main
from uqmc_recovery.engine import ACTION_STATES
from uqmc_recovery.journal import Journal, JournalBusy
from uqmc_recovery.model import Approval, FOLDER, Operation, Plan, SafetyError, digest


class SimulatedProcessDeath(BaseException):
    pass


class FakeNativeTransport:
    def __init__(self, files):
        self.files = copy.deepcopy(files)
        self.apply_enabled = True
        self.updates = []
        self.on_update = None
        self.after_update = None
        self.get_count = 0
        self.on_get = None

    def get_file(self, file_id):
        self.get_count += 1
        if self.on_get:
            self.on_get(file_id, self.get_count)
        return copy.deepcopy(self.files[file_id])

    def list_permissions(self, file_id):
        return copy.deepcopy(self.files[file_id]["permissions"])

    def list_children(self, parent_id):
        return [copy.deepcopy(row) for row in self.files.values() if parent_id in row.get("parents", [])]

    def update_file(self, args):
        self.updates.append(copy.deepcopy(args))
        if self.on_update:
            self.on_update()
        item = self.files[args["fileId"]]
        if "name" in args:
            item["name"] = args["name"]
        if "addParents" in args or "removeParents" in args:
            item["parents"] = sorted((set(item["parents"]) - set(args.get("removeParents", "").split(","))) |
                                     (set(args.get("addParents", "").split(",")) - {""}))
        item["modifiedTime"] = "2026-01-01T00:00:01Z"
        item["version"] = str(int(item["version"]) + 1)
        if self.after_update:
            self.after_update(item)
        return copy.deepcopy(item)


class DirectRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / "state")
        permission = {"id": "fixture-permission", "type": "user", "role": "writer",
                      "emailAddress": "fixture@example.invalid", "domain": None,
                      "displayName": "Fixture", "allowFileDiscovery": None,
                      "permissionDetails": [{"inherited": False}]}
        legacy_permission = {key: permission[key] for key in
                             ("type", "role", "emailAddress", "domain", "displayName", "allowFileDiscovery")}
        self.files = {
            "fixture-source": {"id": "fixture-source", "name": "Original.pdf", "mimeType": "application/pdf",
                               "parents": ["fixture-parent"], "modifiedTime": "2026-01-01T00:00:00Z",
                               "version": "1", "size": "100", "md5Checksum": "fixture-md5",
                               "trashed": False, "permissions": [permission]},
            "fixture-parent": {"id": "fixture-parent", "name": "Fixture parent", "mimeType": FOLDER,
                               "parents": [], "modifiedTime": "2026-01-01T00:00:00Z",
                               "version": "1", "trashed": False, "permissions": [permission]},
        }
        self.before = agent_snapshot({"id": "fixture-source", "name": "Original.pdf", "mime_type": "application/pdf",
            "parents": ["fixture-parent"], "parents_complete": True,
            "modified_time": "2026-01-01T00:00:00Z", "drive_id": None,
            "permissions": [legacy_permission]})
        parent = agent_snapshot({"id": "fixture-parent", "name": "Fixture parent", "mime_type": FOLDER,
            "parents": [], "parents_complete": True, "modified_time": "2026-01-01T00:00:00Z", "drive_id": None,
            "permissions": [legacy_permission], "subtree": {"complete": True, "membership_scope": "DIRECT_CHILDREN",
                "members": [{"id": "fixture-source", "name": "Original.pdf", "mime_type": "application/pdf", "parents": ["fixture-parent"]}]}})
        self.plan = Plan("fixture-direct-run", "fixture-direct-run", "fixture-revision", (
            Operation("fixture:rename", "fixture-source", "RENAME", self.before, {"name": "Renamed.pdf"}),))
        register_agent_plan(self.journal, self.plan)
        clock = datetime.now(timezone.utc)
        approval = Approval("fixture-authority", self.plan.run_id, self.plan.batch_id, self.plan.proposal_revision,
            self.plan.proposal_hash, ("fixture:rename",), "fixture-user", clock.isoformat(),
            (clock+timedelta(hours=1)).isoformat(), "AGENT_ASSISTED")
        auth = {"kind": "EXPLICIT_BATCH_AUTHORIZATION", "authority": "human_user", "authorized": True,
            "actor": "fixture-user", "approval_hash": digest(approval.body()), "proposal_hash": self.plan.proposal_hash,
            "limitations_acknowledged": sorted(AGENT_LIMITATIONS), "access_policy": "SCOPED_EXPOSURE_ACCEPTED",
            "provenance_ref": self.journal.evidence({"kind": "TRUSTED_USER_INSTRUCTION", "text": "Synthetic fixture authority only."}),
            "exposure_review_ref": self.journal.evidence({"kind": "SCOPED_EXPOSURE_REVIEW", "proposal_hash": self.plan.proposal_hash,
                "operation_ids": ["fixture:rename"], "warning_presented": True, "approved": True,
                "newly_exposed_principals": [], "unknown_exposure": True,
                "access_baselines": {value["id"]: self.journal.evidence(value) for value in [self.before, parent]}})}
        enable = {"kind": "DISPATCH_ENABLE_REVIEW", "controller_id": "fixture-previous-controller",
            "proposal_hash": self.plan.proposal_hash, "approval_id": approval.approval_id,
            "authorization_hash": digest(auth), "manifest_verified": True, "reviewer": "fixture-reviewer",
            "reviewed_at": clock.isoformat(), "expires_at": approval.expires_at,
            "provenance_ref": self.journal.evidence({"kind": "TRUSTED_CONTROLLER_REVIEW", "test_only": True})}
        bridge = ActionBridge(self.journal, "AGENT_ASSISTED", controller_id="fixture-previous-controller")
        bridge.authorize_agent_dispatch(self.plan, approval, auth, enable)
        bridge.disable_agent_dispatch("FIXTURE_PAUSED")
        self.transport = FakeNativeTransport(self.files)
        self.runner = DirectRunner(self.journal, self.plan, self.transport, controller_id="fixture-direct-controller")

    def tearDown(self):
        self.temp.cleanup()

    def latest(self):
        return [row for row in self.journal.records() if row["operation_id"] == "fixture:rename" and row["kind"] in ACTION_STATES][-1]

    def apply(self):
        return self.runner.apply(confirm_plan_sha256=self.plan.proposal_hash)

    def test_preview_never_enables_or_mutates(self):
        previous = self.journal.records()
        result = self.runner.preview()
        self.assertTrue(result["read_only"])
        self.assertFalse(result["blocked"])
        self.assertFalse(result["execution_approved"])
        self.assertEqual(self.transport.updates, [])
        self.assertEqual(previous, self.journal.records())

    def test_success_has_durable_attempt_and_single_writer_then_verified_readback(self):
        second = Journal(self.journal.root)
        def at_update():
            self.assertEqual(self.latest()["kind"], "ATTEMPTED")
            # The other Journal has a distinct OS lock descriptor, even in this process.
            with self.assertRaises(JournalBusy):
                with second.locked():
                    pass
        self.transport.on_update = at_update
        result = self.apply()
        self.assertEqual(result["completed_this_invocation"], 1)
        self.assertEqual(self.transport.updates, [{"fileId": "fixture-source", "name": "Renamed.pdf"}])
        self.assertEqual(self.latest()["kind"], "RECONCILED_AT_TARGET")
        observed = self.journal.read_evidence(self.latest()["payload"]["observed_ref"])
        self.assertEqual(observed["name"], "Renamed.pdf")
        self.assertEqual(observed["size"], "100")
        self.assertEqual(self.journal.records()[-1]["kind"], "HANDOFF_DISPATCH_DISABLED")
        self.assertEqual(self.apply()["completed_this_invocation"], 0)
        self.assertEqual(len(self.transport.updates), 1)

    def test_native_content_and_permission_post_drift_blocks(self):
        # Separate fresh contexts keep each injected failure isolated.
        native_permissions = copy.deepcopy(self.files["fixture-source"]["permissions"])
        # This detail is deliberately absent from the historical six-field projection.
        native_permissions[0]["permissionDetails"][0]["inherited"] = True
        for key, value in (("size", "101"), ("md5Checksum", "different-md5"), ("permissions", native_permissions)):
            with self.subTest(field=key):
                if key != "size":
                    self.tearDown()
                    self.setUp()
                self.transport.after_update = lambda item, k=key, v=value: item.__setitem__(k, v)
                with self.assertRaisesRegex(SafetyError, "DIRECT_NATIVE_(SOURCE|PERMISSION)_DRIFT"):
                    self.apply()
                self.assertEqual(self.latest()["kind"], "BLOCKED_CONFLICT")
                self.assertEqual(len(self.transport.updates), 1)
                with self.assertRaisesRegex(SafetyError, "UNRESOLVED_PRIOR_REQUEST"):
                    self.apply()
                self.assertEqual(len(self.transport.updates), 1)

    def test_native_md5_change_between_prepare_and_mark_prevents_write(self):
        reads = []
        def drift(file_id, count):
            if file_id == "fixture-source":
                reads.append(file_id)
                if len(reads) == 2:
                    self.transport.files[file_id]["md5Checksum"] = "manual-content-drift"
        self.transport.on_get = drift
        with self.assertRaisesRegex(SafetyError, "PRE_DISPATCH_DRIFT"):
            self.apply()
        self.assertEqual(self.transport.updates, [])
        self.assertEqual(self.latest()["kind"], "PREPARED")

    def test_move_preserves_known_direct_grants_and_parent_metadata(self):
        op = Operation("fixture:move", "fixture-source", "MOVE", self.before,
                       {"parents": ["fixture-destination"]})
        source = {"file": copy.deepcopy(self.files["fixture-source"]),
                  "permissions": copy.deepcopy(self.files["fixture-source"]["permissions"])}
        parent = {"file": copy.deepcopy(self.files["fixture-parent"]),
                  "permissions": copy.deepcopy(self.files["fixture-parent"]["permissions"])}
        parent["file"]["owners"] = [{"emailAddress": "fixture-owner@example.invalid"}]
        before = {"native_source": source, "native_parents": {"fixture-parent": parent}}
        DirectRunner.native_postcheck(op, before, copy.deepcopy(before))

        for change in ("remove", "role"):
            with self.subTest(direct_grant=change):
                after = copy.deepcopy(before)
                permissions = after["native_source"]["permissions"]
                if change == "remove":
                    permissions.clear()
                else:
                    permissions[0]["role"] = "reader"
                with self.assertRaisesRegex(SafetyError, "DIRECT_NATIVE_DIRECT_GRANT_DRIFT"):
                    DirectRunner.native_postcheck(op, before, after)

        # An explicitly inherited grant may change under the scoped move approval.
        inherited = copy.deepcopy(before)
        inherited["native_source"]["permissions"][0]["permissionDetails"][0]["inherited"] = True
        after = copy.deepcopy(inherited)
        after["native_source"]["permissions"] = []
        DirectRunner.native_postcheck(op, inherited, after)

        changes = {"id": "different-parent", "name": "Manual parent rename",
                   "mimeType": "application/pdf", "parents": ["different-ancestor"],
                   "owners": [{"emailAddress": "different-owner@example.invalid"}]}
        for key, value in changes.items():
            with self.subTest(parent_field=key):
                after = copy.deepcopy(before)
                after["native_parents"]["fixture-parent"]["file"][key] = value
                with self.assertRaisesRegex(SafetyError, "DIRECT_NATIVE_PARENT_DRIFT"):
                    DirectRunner.native_postcheck(op, before, after)

    def test_timeout_after_remote_success_stays_uncertain_at_target_or_preimage(self):
        def lost_response(item):
            raise TimeoutError("synthetic transport timeout")
        self.transport.after_update = lost_response
        with self.assertRaisesRegex(SafetyError, "REQUIRES_RECONCILIATION"):
            self.apply()
        self.assertEqual(self.latest()["kind"], "UNCERTAIN")
        self.assertEqual(len(self.transport.updates), 1)
        # A fresh runner must recover the fence from disk rather than in-memory state.
        self.runner = DirectRunner(self.journal, self.plan, self.transport, controller_id="fixture-resumed-controller")
        for name, relation in (("Renamed.pdf", "AT_TARGET"), ("Original.pdf", "AT_PREIMAGE")):
            with self.subTest(relation=relation):
                self.transport.files["fixture-source"]["name"] = name
                result = self.runner.preview()
                self.assertTrue(result["blocked"])
                self.assertEqual(result["observed_relation"], relation)
                self.assertEqual(result["reason_code"], "REQUEST_NOT_PROVEN_TERMINAL")
                self.assertEqual(self.latest()["kind"], "UNCERTAIN")
                with self.assertRaisesRegex(SafetyError, "UNRESOLVED_PRIOR_REQUEST"):
                    self.apply()
                self.assertEqual(len(self.transport.updates), 1)

    def test_process_death_after_remote_success_cannot_reissue_attempt(self):
        self.transport.after_update = lambda item: (_ for _ in ()).throw(SimulatedProcessDeath())
        with self.assertRaises(SimulatedProcessDeath):
            self.apply()
        self.assertEqual(self.latest()["kind"], "ATTEMPTED")
        self.assertEqual(self.transport.files["fixture-source"]["name"], "Renamed.pdf")
        with self.assertRaisesRegex(SafetyError, "UNRESOLVED_PRIOR_REQUEST"):
            self.apply()
        self.assertEqual(len(self.transport.updates), 1)

    def test_export_failure_does_not_erase_verified_action_or_retry(self):
        self.apply()
        plan_path = self.root / "fixture-plan.json"
        plan_path.write_text(json.dumps(self.plan.body()))
        with patch("uqmc_recovery.direct.export_status", side_effect=OSError("synthetic disk error")), patch("builtins.print") as output:
            code = main(["status", "--store", str(self.journal.root), "--plan", str(plan_path),
                         "--identity-basis", "fixture-identity.json", "--export-dir", str(self.root / "exports")])
        self.assertEqual(code, 0)
        records = [json.loads(call.args[0]) for call in output.call_args_list]
        self.assertEqual(records[0]["completed"], 1)
        self.assertEqual(records[-1], {"status_export_failed": "OSError", "actions_not_retried": True})
        self.assertEqual(self.latest()["kind"], "RECONCILED_AT_TARGET")
        self.assertEqual(len(self.transport.updates), 1)


if __name__ == "__main__":
    unittest.main()
