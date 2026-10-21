"""End-to-end desired-state recovery tests; fake Drive and temporary journals only."""
import copy
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import test_direct_runner as established
from uqmc_recovery.action_bridge import ActionBridge, AGENT_LIMITATIONS
from uqmc_recovery.agent_profile import agent_snapshot, register_agent_plan
from uqmc_recovery.direct_transport import TransportError
from uqmc_recovery.journal import Journal, JournalBusy
from uqmc_recovery.model import Approval, FOLDER, Operation, Plan, SafetyError, digest
from uqmc_recovery.desired import DesiredRunner, compile_items, core, export_status


class ProcessDeath(BaseException):
    pass


class FakeDrive:
    """Absolute metadata PATCH, with failures before or after remote application."""
    def __init__(self, files):
        self.files = copy.deepcopy(files)
        self.apply_enabled = True
        self.calls = []
        self.on_get = None
        self.before_update = None
        self.after_update = None
        self.reads = {}
        self.permission_reads = {}
        self.children_reads = {}
        for item in self.files.values():
            item.setdefault("version", "1")
            item.setdefault("_http_etag", '"fixture-v1"')

    def get_file(self, file_id):
        self.reads[file_id] = self.reads.get(file_id, 0) + 1
        if self.on_get:
            self.on_get(file_id, self.reads[file_id])
        return copy.deepcopy(self.files[file_id])

    def list_permissions(self, file_id):
        self.permission_reads[file_id] = self.permission_reads.get(file_id, 0) + 1
        return copy.deepcopy(self.files[file_id]["permissions"])

    def list_children(self, parent_id):
        self.children_reads[parent_id] = self.children_reads.get(parent_id, 0) + 1
        return [copy.deepcopy(item) for item in self.files.values()
                if parent_id in item.get("parents", []) and not item.get("trashed", False)]

    def update_metadata(self, args, *, etag=None):
        self.calls.append(copy.deepcopy(args))
        if self.before_update:
            self.before_update(args)
        item = self.files[args["fileId"]]
        if etag is not None and etag != item.get("_http_etag"):
            raise TransportError("DRIVE_HTTP_ERROR", status=412, outcome="UNKNOWN")
        if "name" in args:
            item["name"] = args["name"]
        added = set(filter(None, args.get("addParents", "").split(",")))
        removed = set(filter(None, args.get("removeParents", "").split(",")))
        item["parents"] = sorted((set(item["parents"]) - removed) | added)
        item["version"] = str(int(item["version"]) + 1)
        item["_http_etag"] = '"fixture-v' + item["version"] + '"'
        item["modifiedTime"] = "2026-01-01T00:00:01Z"
        if self.after_update:
            self.after_update(item)
        return copy.deepcopy(item)


class DesiredStateTests(unittest.TestCase):
    def test_accepted_section_can_skip_all_permission_lists_for_preview_apply_and_rollback(self):
        self.freeze()
        runner = self.runner(audit_permissions=False)
        # Permission changes are explicitly outside this mode's audit.
        self.transport.files["fixture-source"]["permissions"] = []
        preview = runner.preview()
        self.assertEqual(preview["counts"], {"NOT_APPLIED": 1})
        self.assertFalse(preview["audit_permissions"])
        result = self.run_plan(runner)
        self.assertEqual(result["counts"], {"APPLIED": 1})
        self.assertEqual(self.transport.calls[0], {"fileId": "fixture-source", "name": "Renamed.pdf",
            "addParents": "fixture-destination", "removeParents": "fixture-parent"})
        self.assertEqual(self.run_plan(runner, rollback=True)["counts"], {"ROLLED_BACK": 1})
        self.assertEqual(self.transport.permission_reads, {})
        self.assertTrue(all(r["payload"]["audit_permissions"] is False
            for r in self.journal.records() if r["kind"] == "DESIRED_ATTEMPTED"))

    def test_skipping_permission_audit_requires_existing_scoped_exposure_acceptance(self):
        self.freeze()
        runner = self.runner(audit_permissions=False)
        auth_ref = next(r["payload"]["authorization_ref"] for r in reversed(self.journal.records())
            if r["kind"] == "HANDOFF_AUTHORIZED")
        original_read = self.journal.read_evidence
        def without_exposure_acceptance(ref):
            value = original_read(ref)
            return value | {"access_policy": "PRESERVE_ACCESS"} if ref == auth_ref else value
        with patch.object(self.journal, "read_evidence", side_effect=without_exposure_acceptance):
            with self.assertRaisesRegex(SafetyError, "SKIP_PERMISSION_AUDIT_REQUIRES_ACCEPTED_SECTION_EXPOSURE"):
                runner.preview()
        self.assertEqual(self.transport.reads, {})
        self.assertEqual(self.transport.calls, [])

    def test_rename_only_without_permission_audit_keeps_identity_and_capability_checks(self):
        self.freeze()
        runner = self.runner(audit_permissions=False)
        item = runner.items[0]
        def rename_only():
            raw, _, _ = runner.observe(item)
            return runner.transition(item, raw, core(raw) | {"name": item["target"]["name"]})
        with self.journal.locked():
            self.transport.files["fixture-source"]["capabilities"] = {"canRename": False}
            with self.assertRaisesRegex(SafetyError, "SOURCE_CANNOT_RENAME"):
                rename_only()
            self.transport.files["fixture-source"]["capabilities"] = {"canRename": True}
            self.transport.files["fixture-source"]["name"] = "Human edit.pdf"
            with self.assertRaisesRegex(SafetyError, "UNAPPROVED_NAME_DRIFT"):
                rename_only()
            self.transport.files["fixture-source"]["name"] = "Original.pdf"
            self.assertEqual(rename_only()["state"], "APPLIED")
        self.assertEqual(self.transport.calls, [{"fileId": "fixture-source", "name": "Renamed.pdf"}])
        self.assertEqual(self.transport.files["fixture-source"]["parents"], ["fixture-parent"])
        self.assertEqual(self.transport.permission_reads, {})

    def test_preview_shared_parent_request_count_and_run_scope(self):
        for number in range(9):
            self.add_file("fixture-extra-" + str(number), "Extra " + str(number) + ".pdf")
        operations = list(self.operations())
        for number in range(9):
            operations.extend(self.operations("fixture-extra-" + str(number),
                target_name="Approved " + str(number) + ".pdf"))
        runner = self.freeze(operations)
        before = self.journal.path.read_bytes()
        # Uncached preflight is the original seven-read/item behavior.
        for item in runner.items:
            raw, _, _ = runner.observe(item)
            runner.preflight(item, raw, item["target"])
        def count():
            return sum(self.transport.reads.values()) + sum(self.transport.permission_reads.values()) + sum(self.transport.children_reads.values())
        self.assertEqual(count(), 70)
        self.transport.reads.clear()
        self.transport.permission_reads.clear()
        self.transport.children_reads.clear()
        self.assertEqual(runner.preview()["counts"], {"NOT_APPLIED": 10})
        self.assertEqual(count(), 25)  # 20 source + four parent + one listing.
        self.assertEqual(self.transport.children_reads, {"fixture-destination": 1})
        self.assertEqual(self.transport.reads["fixture-parent"], 1)
        self.assertEqual(self.transport.permission_reads["fixture-destination"], 1)
        runner.preview()
        self.assertEqual(count(), 50)  # No values survive a second invocation.
        self.assertEqual(self.journal.path.read_bytes(), before)
        self.assertEqual(self.transport.calls, [])

    def test_preview_cache_never_authorizes_apply_after_drift(self):
        runner = self.freeze()
        self.assertEqual(runner.preview()["counts"], {"NOT_APPLIED": 1})
        self.transport.files["fixture-destination"]["capabilities"] = {"canAddChildren": False}
        result = self.run_plan(runner)
        self.assertEqual(result["items"][0]["reason"], "DESTINATION_CANNOT_ADD_CHILDREN")
        self.transport.files["fixture-destination"]["capabilities"] = {}
        intruder = copy.deepcopy(self.transport.files["fixture-source"])
        intruder.update(id="fixture-intruder", name="Renamed.pdf", parents=["fixture-destination"])
        self.transport.files["fixture-intruder"] = intruder
        result = self.run_plan(runner)
        self.assertEqual(result["items"][0]["reason"], "UNAPPROVED_SIBLING_COLLISION")
        self.transport.files.pop("fixture-intruder")
        self.transport.files["fixture-source"]["name"] = "Human edit.pdf"
        result = self.run_plan(runner)
        self.assertEqual(result["items"][0]["reason"], "UNAPPROVED_NAME_DRIFT")
        self.assertEqual(self.transport.calls, [])

    def test_apply_after_success_refreshes_parent_and_membership(self):
        self.add_file()
        runner = self.freeze((*self.operations(), *self.operations("fixture-other", target_name="Other approved.pdf")))
        runner.preview()
        self.transport.children_reads.clear()
        def changed_after_first(event):
            if event["event"] == "item_finished" and event["state"] == "APPLIED":
                intruder = copy.deepcopy(self.transport.files["fixture-source"])
                intruder.update(id="fixture-intruder", name="Other approved.pdf")
                self.transport.files["fixture-intruder"] = intruder
        runner.progress = changed_after_first
        result = self.run_plan(runner)
        self.assertEqual(self.states(result), {"fixture-source": "APPLIED", "fixture-other": "CONFLICT"})
        self.assertEqual(result["items"][1]["reason"], "UNAPPROVED_SIBLING_COLLISION")
        self.assertEqual(self.transport.children_reads, {"fixture-destination": 2})
        self.assertEqual(self.transport.reads["fixture-destination"], 3)  # Preview + two apply checks.
        self.assertEqual(len(self.transport.calls), 1)

    def test_preview_limit_and_stop_keep_completed_progress_and_journal(self):
        self.add_file()
        self.freeze(self.operations() + self.operations("fixture-other", target_name="Other approved.pdf"))
        before = self.journal.path.read_bytes()
        events = []
        runner = self.runner(progress=events.append)
        result = runner.preview(limit=1)
        self.assertEqual(result["processed_items"], 1)
        self.assertEqual([e["event"] for e in events], ["item_started", "item_finished"])
        self.assertNotIn("fixture-other", self.transport.reads)
        self.assertEqual(self.journal.path.read_bytes(), before)
        events.clear()
        def stop_after_one(event):
            events.append(event)
            if event["event"] == "item_finished":
                runner.stop_requested = True
        runner.progress = stop_after_one
        result = runner.preview()
        self.assertEqual(result["processed_items"], 1)
        self.assertTrue(result["paused"])
        self.assertEqual(self.journal.path.read_bytes(), before)
        self.assertEqual(self.transport.calls, [])

    def test_preview_timeout_is_reported_with_item_before_final_summary(self):
        self.freeze()
        events = []
        def timeout(source_id, count):
            raise TransportError("DRIVE_REQUEST_TIMEOUT", outcome="READ_FAILED")
        self.transport.on_get = timeout
        result = self.runner(progress=events.append).preview()
        self.assertEqual([e["event"] for e in events], ["item_started", "item_finished"])
        self.assertEqual(events[1]["source_id"], "fixture-source")
        self.assertEqual(events[1]["request_error"]["code"], "DRIVE_REQUEST_TIMEOUT")
        self.assertEqual(result["counts"], {"UNAVAILABLE": 1})
        self.assertEqual(self.transport.calls, [])

    def test_actual_capability_refusal_blocks_only_needed_effect(self):
        self.freeze()
        for source_caps, destination_caps, reason in (
            ({"canRename": False}, {}, "SOURCE_CANNOT_RENAME"),
            ({"canMoveItemWithinDrive": False}, {}, "SOURCE_CANNOT_MOVE"),
            ({}, {"canAddChildren": False}, "DESTINATION_CANNOT_ADD_CHILDREN"),
        ):
            with self.subTest(reason=reason):
                self.transport.files["fixture-source"]["capabilities"] = source_caps
                self.transport.files["fixture-destination"]["capabilities"] = destination_caps
                result = self.run_plan()
                self.assertIn(reason, result["items"][0]["reason"])
                self.assertEqual(self.transport.calls, [])
        self.transport.files["fixture-destination"]["capabilities"] = {}
        self.transport.files["fixture-source"].update(name="Renamed.pdf", capabilities={"canRename": False})
        result = self.run_plan()
        self.assertEqual(result["items"][0]["state"], "APPLIED")
        self.assertNotIn("name", self.transport.calls[0])

    def setUp(self):
        # Compose the established authority fixture; do not inherit its tests.
        self.fixture = established.DirectRunnerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.journal = self.fixture.journal
        self.files = copy.deepcopy(self.fixture.files)
        self.files["fixture-destination"] = copy.deepcopy(self.files["fixture-parent"])
        self.files["fixture-destination"].update(id="fixture-destination", name="Destination")
        self.plan = None
        self.transport = None

    def snapshot(self, source_id):
        raw = self.files[source_id]
        keys = ("type", "role", "emailAddress", "domain", "displayName", "allowFileDiscovery")
        value = {"id": raw["id"], "name": raw["name"], "mime_type": raw["mimeType"],
                 "parents": raw["parents"], "parents_complete": True,
                 "modified_time": raw["modifiedTime"], "drive_id": None,
                 "permissions": [{key: p.get(key) for key in keys} for p in raw["permissions"]]}
        if raw["mimeType"] == FOLDER:
            value["subtree"] = {"complete": True, "membership_scope": "DESCENDANTS",
                "members": [{"id": item["id"], "name": item["name"], "mime_type": item["mimeType"],
                             "parents": item["parents"]} for item in self.files.values()
                            if source_id in item.get("parents", [])]}
        return agent_snapshot(value)

    def operations(self, source_id="fixture-source", *, target_name="Renamed.pdf", target_parent="fixture-destination"):
        before = self.snapshot(source_id)
        rename = Operation(source_id + ":rename", source_id, "RENAME", before, {"name": target_name})
        move = Operation(source_id + ":move", source_id, "MOVE", rename.intended(),
                         {"parents": [target_parent]}, (rename.operation_id,))
        return rename, move

    def add_file(self, source_id="fixture-other", name="Other.pdf", parent="fixture-parent"):
        self.files[source_id] = copy.deepcopy(self.files["fixture-source"])
        self.files[source_id].update(id=source_id, name=name, parents=[parent])

    def freeze(self, operations=None):
        operations = tuple(operations or self.operations())
        self.plan = Plan("fixture-desired-run", "fixture-desired-run", "fixture-r1", operations)
        register_agent_plan(self.journal, self.plan)
        clock = datetime.now(timezone.utc)
        approval = Approval("fixture-desired-authority", self.plan.run_id, self.plan.batch_id,
            self.plan.proposal_revision, self.plan.proposal_hash, tuple(o.operation_id for o in operations),
            "fixture-user", clock.isoformat(), (clock + timedelta(hours=1)).isoformat(), "AGENT_ASSISTED")
        auth = {"kind": "EXPLICIT_BATCH_AUTHORIZATION", "authority": "human_user", "authorized": True,
            "actor": "fixture-user", "approval_hash": digest(approval.body()), "proposal_hash": self.plan.proposal_hash,
            "limitations_acknowledged": sorted(AGENT_LIMITATIONS), "access_policy": "SCOPED_EXPOSURE_ACCEPTED",
            "provenance_ref": self.journal.evidence({"kind": "TRUSTED_USER_INSTRUCTION", "text": "Synthetic desired-state fixture approval."}),
            "exposure_review_ref": self.journal.evidence({"kind": "SCOPED_EXPOSURE_REVIEW", "proposal_hash": self.plan.proposal_hash,
                "operation_ids": [o.operation_id for o in operations], "warning_presented": True, "approved": True,
                "newly_exposed_principals": [], "unknown_exposure": True,
                "access_baselines": {key: self.journal.evidence(self.snapshot(key)) for key in self.files}})}
        enable = {"kind": "DISPATCH_ENABLE_REVIEW", "controller_id": "fixture-old-controller",
            "proposal_hash": self.plan.proposal_hash, "approval_id": approval.approval_id,
            "authorization_hash": digest(auth), "manifest_verified": True, "reviewer": "fixture-reviewer",
            "reviewed_at": clock.isoformat(), "expires_at": approval.expires_at,
            "provenance_ref": self.journal.evidence({"kind": "TRUSTED_CONTROLLER_REVIEW", "test_only": True})}
        bridge = ActionBridge(self.journal, "AGENT_ASSISTED", controller_id="fixture-old-controller")
        bridge.authorize_agent_dispatch(self.plan, approval, auth, enable)
        bridge.disable_agent_dispatch("FIXTURE_PAUSED")
        self.transport = FakeDrive(self.files)
        return self.runner()

    def runner(self, **kwargs):
        return DesiredRunner(self.journal, self.plan, self.transport, retry_delay=0, **kwargs)

    def run_plan(self, runner=None, **kwargs):
        return (runner or self.runner()).run(apply=True, confirm_plan_sha256=self.plan.proposal_hash, **kwargs)

    @staticmethod
    def states(result):
        return {item["source_id"]: item["state"] for item in result["items"]}

    def historical(self, operation, kind="RECONCILED_AT_TARGET"):
        observed = operation.intended() if kind == "RECONCILED_AT_TARGET" else operation.before
        self.journal.append(kind, operation_id=operation.operation_id, attempt=1, payload={
            "run_id": self.plan.run_id, "proposal_hash": self.plan.proposal_hash,
            "file_id": operation.file_id, "action": operation.action, "direction": "FORWARD",
            "depends_on": list(operation.depends_on), "proposal_refs": list(operation.proposal_refs),
            "before_ref": self.journal.evidence(operation.before),
            "intended_ref": self.journal.evidence(operation.intended()),
            "observed_ref": self.journal.evidence(observed), "reason_code": "SYNTHETIC_HISTORY"})

    def test_preview_is_read_only_and_combined_call_has_write_ahead_lock(self):
        runner = self.freeze()
        previous = self.journal.records()
        self.assertEqual(self.states(runner.preview())["fixture-source"], "NOT_APPLIED")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.journal.records(), previous)
        other = Journal(self.journal.root)
        def assert_durable(args):
            self.assertTrue(any(row["kind"] == "DESIRED_ATTEMPTED" for row in self.journal.records()))
            with self.assertRaises(JournalBusy):
                with other.locked():
                    pass
        self.transport.before_update = assert_durable
        result = self.run_plan()
        self.assertEqual(self.states(result)["fixture-source"], "APPLIED")
        self.assertEqual(self.transport.calls, [{"fileId": "fixture-source", "name": "Renamed.pdf",
            "addParents": "fixture-destination", "removeParents": "fixture-parent"}])
        self.assertEqual(self.states(self.run_plan())["fixture-source"], "TARGET")
        self.assertEqual(len(self.transport.calls), 1)

    def test_already_moved_only_renames_and_export_keeps_item_order(self):
        self.freeze()
        self.transport.files["fixture-source"]["parents"] = ["fixture-destination"]
        result = self.run_plan()
        self.assertEqual(self.states(result)["fixture-source"], "APPLIED")
        self.assertEqual(self.transport.calls, [{"fileId": "fixture-source", "name": "Renamed.pdf"}])
        exported = export_status(self.journal, result, self.fixture.root / "exports")
        self.assertFalse(exported["published"])
        log = json.loads(Path(exported["operation_log"]).read_text())
        self.assertGreaterEqual(len(log["rows"]), 3)
        self.assertEqual({row[5] for row in log["rows"]}, {1})
        self.assertGreater(max(r["seq"] for r in self.journal.records()), 1)

    def test_existing_verified_rename_only_moves_and_rollback_preserves_rename(self):
        self.freeze()
        rename = self.plan.operations[0]
        self.historical(rename)
        self.transport.files["fixture-source"]["name"] = "Renamed.pdf"
        result = self.run_plan()
        self.assertEqual(self.states(result)["fixture-source"], "APPLIED")
        self.assertEqual(self.transport.calls[0], {"fileId": "fixture-source", "addParents": "fixture-destination", "removeParents": "fixture-parent"})
        self.run_plan(rollback=True)
        self.assertEqual(self.transport.files["fixture-source"]["name"], "Renamed.pdf")
        self.assertEqual(self.transport.files["fixture-source"]["parents"], ["fixture-parent"])
        self.assertNotIn("name", self.transport.calls[-1])

    def test_historical_verified_rename_reversal_is_not_silently_replayed(self):
        self.freeze()
        self.historical(self.plan.operations[0])
        result = self.run_plan()
        self.assertEqual(self.states(result)["fixture-source"], "CONFLICT")
        self.assertEqual(self.transport.calls, [])

    def test_current_preimage_is_known_despite_historical_uncertain_request(self):
        self.freeze()
        self.historical(self.plan.operations[0], "UNCERTAIN")
        preview = self.runner().preview()
        self.assertEqual(self.states(preview)["fixture-source"], "NOT_APPLIED")
        self.assertEqual(self.states(self.run_plan())["fixture-source"], "APPLIED")
        self.assertTrue(any(row["kind"] == "UNCERTAIN" for row in self.journal.records()))
        self.assertEqual(len(self.transport.calls), 1)

    def test_response_lost_after_success_reads_target_without_retry(self):
        self.freeze()
        def timeout(item):
            raise TimeoutError("fake lost response")
        self.transport.after_update = timeout
        events = []
        result = self.run_plan(self.runner(progress=lambda event: events.append((event, self.transport.reads.get("fixture-source", 0)))))
        failure = next((event, reads) for event, reads in events if event["event"] == "request_failed")
        self.assertEqual(failure[0]["next_step"], "READ_CURRENT_STATE_BEFORE_ANY_RETRY")
        self.assertGreater(self.transport.reads["fixture-source"], failure[1])
        self.assertIn(self.states(result)["fixture-source"], ("APPLIED", "TARGET"))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.states(self.run_plan())["fixture-source"], "TARGET")
        self.assertEqual(len(self.transport.calls), 1)

    def test_timeout_unchanged_state_retries_with_fresh_reads_and_bound(self):
        self.freeze()
        def transient(args):
            if len(self.transport.calls) < 3:
                raise TransportError("DRIVE_REQUEST_TIMEOUT", outcome="UNKNOWN")
        self.transport.before_update = transient
        self.assertEqual(self.states(self.run_plan())["fixture-source"], "APPLIED")
        self.assertEqual(len(self.transport.calls), 3)
        self.assertGreaterEqual(self.transport.reads["fixture-source"], 6)

    def test_exhausted_transient_failure_is_retryable_item_not_global_deadlock(self):
        self.add_file()
        self.freeze((*self.operations(), *self.operations("fixture-other", target_name="Other renamed.pdf")))
        def transient(args):
            if args["fileId"] == "fixture-source":
                raise TransportError("DRIVE_HTTP_ERROR", status=503, outcome="UNKNOWN")
        self.transport.before_update = transient
        result = self.run_plan()
        self.assertEqual(self.states(result)["fixture-source"], "NOT_APPLIED")
        self.assertEqual(self.states(result)["fixture-other"], "APPLIED")
        self.assertEqual(sum(c["fileId"] == "fixture-source" for c in self.transport.calls), 3)
        self.transport.before_update = None
        self.assertEqual(self.states(self.run_plan())["fixture-source"], "APPLIED")

    def test_partial_completion_resume_never_replays_completed_effects(self):
        self.add_file()
        self.freeze((*self.operations(), *self.operations("fixture-other", target_name="Other renamed.pdf")))
        self.run_plan(limit=1)
        self.assertEqual(len(self.transport.calls), 1)
        self.run_plan()
        self.assertEqual(len(self.transport.calls), 2)
        self.run_plan()
        self.assertEqual(len(self.transport.calls), 2)

    def test_interruption_at_each_dispatch_boundary_recovers_without_duplicate_effect(self):
        for stage in ("before_dispatch", "after_dispatch", "after_readback"):
            with self.subTest(stage=stage):
                if self.plan is not None:
                    self.fixture.tearDown()
                    self.setUp()
                self.freeze()
                triggered = []
                def die(point, *args):
                    if point == stage and not triggered:
                        triggered.append(point)
                        raise ProcessDeath()
                with self.assertRaises(ProcessDeath):
                    self.run_plan(self.runner(fault=die))
                self.assertEqual(triggered, [stage])
                result = self.run_plan()
                self.assertIn(self.states(result)["fixture-source"], ("APPLIED", "TARGET"))
                self.assertEqual(len(self.transport.calls), 1)

    def test_interrupted_pending_attempt_rejects_same_core_external_version_drift(self):
        self.freeze()
        def die(point, *args):
            if point == "before_dispatch":
                raise ProcessDeath()
        with self.assertRaises(ProcessDeath):
            self.run_plan(self.runner(fault=die))
        item = self.transport.files["fixture-source"]
        item["version"] = "2"
        item["_http_etag"] = '\"fixture-v2\"'
        item["modifiedTime"] = "2026-01-02T00:00:00Z"
        result = self.run_plan()
        self.assertEqual(self.states(result)["fixture-source"], "CONFLICT")
        self.assertEqual(self.transport.calls, [])

    def test_external_drift_blocks_only_that_item(self):
        self.add_file()
        self.freeze((*self.operations(), *self.operations("fixture-other", target_name="Other renamed.pdf")))
        self.transport.files["fixture-source"]["name"] = "Human edit.pdf"
        states = self.states(self.run_plan())
        self.assertEqual(states["fixture-source"], "CONFLICT")
        self.assertEqual(states["fixture-other"], "APPLIED")
        self.assertEqual([c["fileId"] for c in self.transport.calls], ["fixture-other"])

    def test_drift_between_initial_and_prewrite_read_never_overwrites(self):
        self.freeze()
        def edit(source_id, count):
            if source_id == "fixture-source" and count == 2:
                self.transport.files[source_id]["name"] = "Human edit.pdf"
                self.transport.files[source_id]["version"] = "2"
                self.transport.files[source_id]["_http_etag"] = '"fixture-v2"'
        self.transport.on_get = edit
        self.assertEqual(self.states(self.run_plan())["fixture-source"], "CONFLICT")
        self.assertEqual(self.transport.calls, [])

    def test_conditional_write_rejects_human_change_after_last_read(self):
        self.freeze()
        def edit(args):
            item = self.transport.files[args["fileId"]]
            item["name"] = "Human changed during request.pdf"
            item["version"] = "2"
            item["_http_etag"] = '\"fixture-v2\"'
        self.transport.before_update = edit
        result = self.run_plan()
        self.assertEqual(self.states(result)["fixture-source"], "CONFLICT")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.files["fixture-source"]["name"], "Human changed during request.pdf")

    def test_partial_rollback_restart_and_manual_drift_guard(self):
        self.add_file()
        self.freeze((*self.operations(), *self.operations("fixture-other", target_name="Other renamed.pdf")))
        self.run_plan()
        self.run_plan(rollback=True, limit=1)
        self.assertEqual(len(self.transport.calls), 3)
        self.run_plan(rollback=True)
        self.assertEqual(len(self.transport.calls), 4)
        self.run_plan(rollback=True)
        self.assertEqual(len(self.transport.calls), 4)
        self.assertEqual(self.transport.files["fixture-source"]["name"], "Original.pdf")
        self.assertEqual(self.transport.files["fixture-other"]["name"], "Other.pdf")

    def test_rollback_interruption_before_and_after_dispatch_recovers_once(self):
        for stage in ("before_dispatch", "after_dispatch"):
            with self.subTest(stage=stage):
                if self.plan is not None:
                    self.fixture.tearDown()
                    self.setUp()
                self.freeze()
                self.run_plan()
                triggered = []
                def die(point, *args):
                    if point == stage and not triggered:
                        triggered.append(point)
                        raise ProcessDeath()
                with self.assertRaises(ProcessDeath):
                    self.run_plan(self.runner(fault=die), rollback=True)
                result = self.run_plan(rollback=True)
                self.assertEqual(self.states(result)["fixture-source"], "ROLLED_BACK")
                self.assertEqual(len(self.transport.calls), 2)
                self.assertEqual(self.transport.files["fixture-source"]["name"], "Original.pdf")
                self.assertEqual(self.transport.files["fixture-source"]["parents"], ["fixture-parent"])
                self.run_plan(rollback=True)
                self.assertEqual(len(self.transport.calls), 2)

    def test_lost_rollback_response_is_reconciled_without_reapplying_inverse(self):
        self.freeze()
        self.run_plan()
        def timeout(item):
            raise TimeoutError("fake lost rollback response")
        self.transport.after_update = timeout
        result = self.run_plan(rollback=True)
        self.assertEqual(self.states(result)["fixture-source"], "ROLLED_BACK")
        self.assertEqual(self.transport.files["fixture-source"]["name"], "Original.pdf")
        self.assertEqual(self.transport.files["fixture-source"]["parents"], ["fixture-parent"])
        self.run_plan(rollback=True)
        self.assertEqual(len(self.transport.calls), 2)

    def test_rollback_refuses_manual_change_after_applied_state(self):
        self.freeze()
        self.run_plan()
        self.transport.files["fixture-source"]["name"] = "Human edit after migration.pdf"
        result = self.run_plan(rollback=True)
        self.assertEqual(self.states(result)["fixture-source"], "CONFLICT")
        self.assertEqual(len(self.transport.calls), 1)

    def test_folder_dependency_does_not_preserve_unrelated_global_rename_phase(self):
        self.add_file("fixture-receiver", "Old receiver", parent="fixture-parent")
        self.files["fixture-receiver"]["mimeType"] = FOLDER
        self.files["fixture-receiver"].pop("size", None)
        self.files["fixture-receiver"].pop("md5Checksum", None)
        root_rename, root_move = self.operations("fixture-receiver", target_name="Receiver")
        incoming_rename, incoming_move = self.operations(target_parent="fixture-receiver")
        root_move = Operation(root_move.operation_id, root_move.file_id, root_move.action, root_move.before,
                              root_move.target, (root_rename.operation_id, incoming_rename.operation_id))
        incoming_move = Operation(incoming_move.operation_id, incoming_move.file_id, incoming_move.action,
                                 incoming_move.before, incoming_move.target,
                                 (incoming_rename.operation_id, root_move.operation_id))
        self.freeze((root_rename, incoming_rename, root_move, incoming_move))
        items = compile_items(self.plan, self.journal)
        by_id = {item["source_id"]: item for item in items}
        self.assertIn("fixture-receiver", by_id["fixture-source"]["dependencies"])
        self.assertNotIn("fixture-source", by_id["fixture-receiver"]["dependencies"])
        self.run_plan()
        self.assertEqual([c["fileId"] for c in self.transport.calls], ["fixture-receiver", "fixture-source"])
        self.assertEqual(self.transport.files["fixture-source"]["parents"], ["fixture-receiver"])


if __name__ == "__main__":
    unittest.main()
