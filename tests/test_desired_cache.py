"""Offline cache regressions using synthetic plans, temporary journals and fake Drive."""
import unittest
from unittest.mock import PropertyMock, patch

import test_desired as fixtures
from uqmc_recovery.desired import DesiredRunner
from uqmc_recovery.model import Plan


class DesiredCacheTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DesiredStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_events_filters_by_cached_hash_without_rehashing_the_plan(self):
        self.fixture.freeze()
        runner = self.fixture.runner(audit_permissions=False)
        sealed_hash = self.fixture.plan.proposal_hash
        journal = self.fixture.journal
        expected = journal.append("DESIRED_OBSERVED", payload={
            "plan_sha256": sealed_hash, "source_id": "fixture-source", "state": "NOT_APPLIED"})
        other_source = journal.append("DESIRED_OBSERVED", payload={
            "plan_sha256": sealed_hash, "source_id": "fixture-other", "state": "NOT_APPLIED"})
        journal.append("DESIRED_OBSERVED", payload={
            "plan_sha256": "foreign-plan", "source_id": "fixture-source", "state": "TARGET"})
        journal.append("UNRELATED_EVENT", payload={"plan_sha256": sealed_hash, "source_id": "fixture-source"})
        with patch.object(Plan, "proposal_hash", new_callable=PropertyMock,
                side_effect=AssertionError("Event lookup must use its sealed hash")) as hashes:
            self.assertEqual(runner.events("fixture-source"), [expected])
            self.assertEqual(runner.events(), [expected, other_source])
            self.assertEqual(runner.events("absent-source"), [])
            hashes.assert_not_called()

    def test_caller_nested_plan_mutations_cannot_change_sealed_execution(self):
        self.fixture.freeze()
        caller_plan = self.fixture.plan
        sealed_hash = caller_plan.proposal_hash
        runner = self.fixture.runner(audit_permissions=False)
        caller_plan.operations[0].target["name"] = "Unapproved replacement.pdf"
        caller_plan.operations[1].target["parents"][:] = ["unapproved-destination"]
        caller_plan.operations[0].before["parents"][:] = ["unapproved-original"]
        self.assertNotEqual(caller_plan.proposal_hash, sealed_hash)
        self.assertEqual(runner.plan.proposal_hash, sealed_hash)
        result = runner.run(apply=True, confirm_plan_sha256=sealed_hash, workers=1)
        self.assertEqual(result["counts"], {"APPLIED": 1})
        self.assertEqual(self.fixture.transport.calls, [{"fileId": "fixture-source", "name": "Renamed.pdf",
            "addParents": "fixture-destination", "removeParents": "fixture-parent"}])
        self.assertEqual(result["plan_sha256"], sealed_hash)
        self.assertEqual(self.fixture.transport.permission_reads, {})

    def test_revocation_during_final_fresh_read_prevents_dispatch(self):
        self.fixture.freeze()
        runner = self.fixture.runner(audit_permissions=False)
        approval_id = next(row["payload"]["approval_id"] for row in self.fixture.journal.records()
            if row["kind"] == "HANDOFF_AUTHORIZED"
            and row["payload"].get("proposal_hash") == runner.plan_sha256)
        def revoke_on_final_source_read(source_id, count):
            if source_id == "fixture-source" and count == 2:
                rows = self.fixture.journal.records()
                self.assertTrue(any(row["kind"] == "DESIRED_PREPARED" for row in rows))
                self.assertFalse(any(row["kind"] == "DESIRED_ATTEMPTED" for row in rows))
                self.fixture.journal.append("REVOKED", payload={"approval_id": approval_id})
        self.fixture.transport.on_get = revoke_on_final_source_read
        result = runner.run(apply=True, confirm_plan_sha256=self.fixture.plan.proposal_hash, workers=1)
        self.assertTrue(any(row["kind"] == "REVOKED" and row["payload"].get("approval_id") == approval_id
            for row in self.fixture.journal.records()))
        self.assertEqual(result["counts"], {"CONFLICT": 1})
        self.assertIn("REVOKED", result["items"][0]["reason"])
        self.assertEqual(self.fixture.transport.calls, [])
        rows = self.fixture.journal.records()
        self.assertTrue(any(row["kind"] == "REVOKED" for row in rows))
        self.assertFalse(any(row["kind"] == "DESIRED_ATTEMPTED" for row in rows))

    def test_partial_completion_resumes_without_replaying_verified_effect(self):
        self.fixture.add_file()
        self.fixture.freeze((*self.fixture.operations(),
            *self.fixture.operations("fixture-other", target_name="Other approved.pdf")))
        sealed_hash = self.fixture.plan.proposal_hash
        runner = self.fixture.runner(audit_permissions=False)
        self.fixture.transport.after_update = lambda item: setattr(runner, "stop_requested", True)
        first = runner.run(apply=True, confirm_plan_sha256=sealed_hash, workers=1)
        self.assertEqual(first["counts"], {"APPLIED": 1})
        self.assertTrue(first["paused"])
        self.assertEqual([call["fileId"] for call in self.fixture.transport.calls], ["fixture-source"])
        self.fixture.transport.after_update = None
        resumed = DesiredRunner(self.fixture.journal, self.fixture.plan, self.fixture.transport,
            audit_permissions=False, retry_delay=0)
        second = resumed.run(apply=True, confirm_plan_sha256=sealed_hash, workers=1)
        self.assertEqual(second["counts"], {"TARGET": 1, "APPLIED": 1})
        self.assertEqual([call["fileId"] for call in self.fixture.transport.calls],
            ["fixture-source", "fixture-other"])
        records = self.fixture.journal.records()
        self.assertEqual(sum(row["kind"] == "DESIRED_APPLIED" for row in records), 2)
        self.assertEqual(self.fixture.transport.permission_reads, {})


if __name__ == "__main__":
    unittest.main()
