import copy
import hashlib
import json
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from uqmc_recovery.model import Plan

from uqmc_recovery.projection import (
    HEADERS, WORKBOOK_STATES, build_projections, deliver_projection, delivery_key, source_head,
    validate_snapshot,
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
        # Explicit frozen plan order, independent of journal event sequence.
        order = ["rename-1", "other-op", "move-1", "rename-later", "undo-rename",
                 "folder-move", "undo-folder"]
        plan_ref = self.journal.evidence({"operations": [{"operation_id": op} for op in order]})
        self.journal.append("DRAFTED", payload={"batch_id": "batch-1", "plan_ref": plan_ref})
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
        self.assertEqual(len(output["operation_log"]["rows"]), 1)

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
        self.assertEqual(output["operation_log"]["headers"], HEADERS)
        self.assertTrue(all(len(r) == 26 for r in output["operation_log"]["rows"]))
        self.assertEqual(output["operation_log"]["rows"][0][2], "batch-1")
        self.assertEqual(output["operation_log"]["snapshot_id"], output["workbook"]["snapshot_id"])

    def test_formula_name_remains_literal_for_raw_sheet_insert(self):
        self.add("PREPARED")
        private = build_projections(self.journal)["operation_log"]
        self.assertEqual(private["valueInputOption"], "RAW")
        self.assertEqual(private["rows"][0][12], "=SECRET_FORMULA()")
        self.assertEqual(private["rows"][0][13], '["private-parent"]')

    def test_policy_events_do_not_become_log_rows(self):
        for kind in ("DRAFTED", "APPROVED", "REVOKED", "EXCLUDED"):
            self.add(kind)
        self.assertEqual(build_projections(self.journal)["operation_log"]["rows"], [])

    def test_reconciled_mapping_preserves_attribution_uncertainty(self):
        self.add("RECONCILED_AT_TARGET")
        row = build_projections(self.journal)["operation_log"]["rows"][0]
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
        self.assertIsNone(build_projections(self.journal)["diagnostics"]["stale"])
        result = deliver_projection(self.journal, "html", projection, lambda key, payload: {})
        self.assertEqual(result["state"], "PUBLICATION_FAILED")
        latest = build_projections(self.journal, current_source_head=source_head(self.journal.records()))["diagnostics"]
        self.assertFalse(latest["stale"])
        stale = build_projections(self.journal, current_source_head={"seq": 999, "hash": "x"})["diagnostics"]
        self.assertTrue(stale["stale"])

    def test_frozen_unattempted_plan_is_visible_without_fabricated_observation(self):
        ref = self.journal.evidence({"operations": [
            {"operation_id": "pending", "file_id": "source-private", "action": "MOVE",
             "direction": "FORWARD", "depends_on": [], "before": {"name": "Secret"},
             "target": {"parents": ["private-parent"]}}]})
        self.journal.append("DRAFTED", payload={"run_id": "batch-1", "batch_id": "batch-1",
                                               "proposal_hash": "a" * 64, "plan_ref": ref})
        output = build_projections(self.journal, allowed_source_ids=["source-private"])
        self.assertEqual(output["html"]["operations"], [])
        self.assertEqual(output["workbook"]["operations"], [])
        for surface in ("workbook", "html"):
            item = output[surface]["items"][0]
            for field in ("move", "rename"):
                self.assertEqual(item[field], {"effect": "NOT_TRACKED", "last_event_state": None,
                                               "operation_ids": [], "verified_at_utc": None})
            self.assertEqual(item["location"]["effect"], "UNKNOWN")
            self.assertIsNone(item["observed"]["metadata_verified_at_utc"])
        self.assertEqual(output["html"]["items"][0]["observed"]["read_state"], "NEVER_VERIFIED")
        self.assertEqual(output["operation_log"]["rows"], [])

    def test_export_has_exact_schema_keys_at_every_level(self):
        self.add("PREPARED")
        output = build_projections(self.journal, allowed_source_ids=["source-private"])
        schema = json.loads((Path(__file__).parents[1] / "schemas" /
                             "uqmc.execution.snapshot.v1.schema.json").read_text())

        def check_schema(value, spec):
            if "anyOf" in spec:
                failures = []
                for branch in spec["anyOf"]:
                    try:
                        check_schema(value, branch)
                        return
                    except AssertionError as exc:
                        failures.append(exc)
                self.fail(str(failures))
            kind = spec.get("type")
            types = {"string": str, "object": dict, "array": list,
                     "integer": int, "boolean": bool, "null": type(None)}
            if kind:
                choices = kind if isinstance(kind, list) else [kind]
                self.assertIn(type(value), [types[t] for t in choices])
            if "const" in spec:
                self.assertEqual(value, spec["const"])
            if "enum" in spec:
                self.assertIn(value, spec["enum"])
            if "minimum" in spec:
                self.assertGreaterEqual(value, spec["minimum"])
            if "minLength" in spec:
                self.assertGreaterEqual(len(value), spec["minLength"])
            if spec.get("uniqueItems"):
                self.assertEqual(len(value), len({json.dumps(item, sort_keys=True) for item in value}))
            if isinstance(value, dict):
                self.assertFalse(spec["additionalProperties"])
                self.assertEqual(set(value), set(spec["required"]))
                for key, item in value.items():
                    check_schema(item, spec["properties"][key])
            elif isinstance(value, list):
                for item in value:
                    check_schema(item, spec["items"])

        for surface in ("workbook", "html"):
            validate_snapshot(output[surface])
            check_schema(output[surface], schema)
        self.assertNotIn("stale", output["html"])
        self.assertNotIn("rows", output["workbook"])
        self.assertIsNone(output["html"]["items"][0]["source_link"])

    def test_strict_validator_rejects_additional_nested_fields(self):
        self.add("PREPARED")
        snapshot = build_projections(self.journal)["workbook"]
        paths = [(), ("coverage",), ("operations", 0), ("items", 0),
                 ("items", 0, "move"), ("items", 0, "rename"),
                 ("items", 0, "location"), ("items", 0, "observed")]
        for path in paths:
            with self.subTest(path=path):
                changed = copy.deepcopy(snapshot)
                target = changed
                for part in path:
                    target = target[part]
                target["private_evidence"] = self.before
                with self.assertRaises(ValueError):
                    validate_snapshot(changed)

    def test_strict_validator_rejects_null_order_draft_state_and_invalid_types(self):
        self.add("PREPARED")
        snapshot = build_projections(self.journal)["workbook"]
        changes = [("operations", 0, "operation_order", None),
                   ("operations", 0, "operation_order", 0),
                   ("operations", 0, "operation_order", True),
                   ("operations", 0, "state", "NOT_ATTEMPTED"),
                   ("coverage", "identity_basis_version", 0),
                   ("coverage", "identity_count", -1),
                   ("items", 0, "source_link", 7),
                   ("generated_at_utc", "2026-10-21Z")]
        for *path, key, value in changes:
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(snapshot)
                target = changed
                for part in path:
                    target = target[part]
                target[key] = value
                with self.assertRaises(ValueError):
                    validate_snapshot(changed)

    def test_operation_order_uses_frozen_plan_not_dispatch_sequence(self):
        self.add("PREPARED", op="move-1", action="MOVE", operation_order=999)
        self.add("ATTEMPTED", op="move-1", action="MOVE", operation_order=888)
        self.add("PREPARED")
        output = build_projections(self.journal)
        self.assertEqual([op["operation_order"] for op in output["workbook"]["operations"]], [3, 1])
        self.assertEqual([row[5] for row in output["operation_log"]["rows"]], [3, 3, 1])

    def test_sealed_dependency_order_matches_plan_execution_order(self):
        raw = [{"operation_id": "child", "depends_on": ["base"]},
               {"operation_id": "independent", "depends_on": []},
               {"operation_id": "base", "depends_on": []}]
        plan = Plan("dependency-batch", "dependency-batch", "fixture", tuple(SimpleNamespace(**op) for op in raw))
        expected = {op.operation_id: index for index, op in enumerate(plan.ordered(), 1)}
        ref = self.journal.evidence({"run_id": plan.run_id, "batch_id": plan.batch_id, "operations": raw})
        self.journal.append("DRAFTED", payload={"batch_id": plan.batch_id, "run_id": plan.run_id, "plan_ref": ref})
        for op in raw:
            self.add("PREPARED", op=op["operation_id"], batch_id=plan.batch_id, run_id=plan.run_id)
        output = build_projections(self.journal)
        self.assertEqual(expected, {op["operation_id"]: op["operation_order"] for op in output["workbook"]["operations"]})
        self.assertEqual(expected, {row[3]: row[5] for row in output["operation_log"]["rows"]})
        self.assertEqual(expected, {"independent": 1, "base": 2, "child": 3})

    def test_sealed_invalid_dependencies_or_run_binding_block_export(self):
        plans = [
            {"operations": [{"operation_id": "new", "depends_on": ["missing"]}]},
            {"operations": [{"operation_id": "new", "depends_on": ["new"]}]},
            {"operations": [{"operation_id": "new"}, {"operation_id": "new"}]},
            {"operations": [{"operation_id": "new"}], "run_id": "mismatched"},
            {"operations": [{"operation_id": "new"}], "batch_id": "mismatched"},
        ]
        for plan in plans:
            with self.subTest(plan=plan):
                journal = MemoryJournal()
                ref = journal.evidence(plan)
                journal.append("DRAFTED", payload={"run_id": "batch", "batch_id": "batch", "plan_ref": ref})
                with self.assertRaises(ValueError):
                    build_projections(journal)

    def test_validator_accepts_nullable_operation_source_and_reason(self):
        self.add("PREPARED")
        snapshot = build_projections(self.journal)["workbook"]
        snapshot["operations"][0].update(source_id=None, reason_code=None)
        self.assertIs(validate_snapshot(snapshot), snapshot)

    def test_validator_rejects_empty_required_identifiers(self):
        self.add("PREPARED")
        snapshot = build_projections(self.journal)["workbook"]
        for path in (("operations", 0, "operation_id"), ("operations", 0, "run_id"),
                     ("items", 0, "source_id")):
            with self.subTest(path=path):
                changed = copy.deepcopy(snapshot)
                changed[path[0]][path[1]][path[2]] = ""
                with self.assertRaises(ValueError):
                    validate_snapshot(changed)

    def test_validator_rejects_duplicate_array_values_at_every_level(self):
        self.add("PREPARED")
        snapshot = build_projections(self.journal)["workbook"]
        paths = [("operations",), ("items",), ("operations", 0, "depends_on_operation_ids"),
                 ("items", 0, "reason_codes"), ("items", 0, "move", "operation_ids"),
                 ("items", 0, "rename", "operation_ids"),
                 ("items", 0, "location", "ancestor_move_operation_ids"),
                 ("items", 0, "observed", "parent_ids")]
        for path in paths:
            with self.subTest(path=path):
                changed = copy.deepcopy(snapshot)
                target = changed
                for part in path[:-1]:
                    target = target[part]
                array = target[path[-1]]
                repeated = array[0] if array else "duplicate"
                target[path[-1]] = [repeated, copy.deepcopy(repeated)]
                with self.assertRaises(ValueError):
                    validate_snapshot(changed)

    def test_action_without_sealed_order_blocks_export(self):
        self.add("PREPARED", op="not-in-frozen-plan")
        with self.assertRaisesRegex(ValueError, "no sealed plan"):
            build_projections(self.journal)

    def test_identity_basis_binds_exact_ids_and_public_selection(self):
        ids = ["source-private", "untracked"]
        fingerprint = hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()
        basis = {"identity_basis_version": 7, "source_ids": ids,
                 "identity_count": 2, "source_identity_sha256": fingerprint}
        output = build_projections(self.journal, allowed_source_ids=["source-private"],
                                   identity_basis=basis,
                                   verified_source_links={"source-private": "https://example.test/verified"})
        for surface in ("workbook", "html"):
            self.assertEqual(output[surface]["coverage"]["identity_basis_version"], 7)
            self.assertEqual(output[surface]["coverage"]["identity_count"], 2)
            self.assertEqual(output[surface]["coverage"]["source_identity_sha256"], fingerprint)
        self.assertEqual(output["html"]["items"][0]["source_link"], "https://example.test/verified")
        for broken in ({**basis, "identity_count": 3}, {**basis, "source_identity_sha256": "0" * 64},
                       {**basis, "source_ids": ids + [ids[0]]}):
            with self.assertRaises(ValueError):
                build_projections(self.journal, identity_basis=broken)
        with self.assertRaisesRegex(ValueError, "outside identity basis"):
            build_projections(self.journal, allowed_source_ids=["unknown"], identity_basis=basis)

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
