"""Direct API provenance uses the existing handoff guards; no network or secrets."""
import unittest
from datetime import datetime, timezone

import test_action_bridge as fixtures
from uqmc_recovery.action_bridge import ActionBridge, AGENT_TOOL, DIRECT_API_TOOL
from uqmc_recovery.model import SafetyError, digest


class DirectBridgeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AgentAssistedBridgeTests()
        self.case.setUp()
        self.case.bridge = ActionBridge(
            self.case.journal, "AGENT_ASSISTED",
            controller_id="synthetic-controller", execution_transport="DIRECT_GOOGLE_API")
        self.bridge = self.case.bridge

    def tearDown(self):
        self.case.tearDown()

    def enable_direct(self):
        self.case.enable["execution_transport"] = "DIRECT_GOOGLE_API"
        return self.case.authorize()

    def prepare_direct(self):
        self.enable_direct()
        case = self.case
        return self.bridge.prepare_action(
            case.plan, case.plan.operations[0].operation_id,
            case.approval, case.before, case.check())

    def terminal(self, envelope, tool=DIRECT_API_TOOL):
        case = self.case
        raw = {"transport": "DIRECT_GOOGLE_API",
               "structuredContent": {"id": envelope["file_id"]},
               "provider_response": {"id": envelope["file_id"]}}
        response_ref = self.bridge.ingest_result(
            envelope["envelope_hash"], envelope["attempt_key"], raw)
        completion = {
            "kind": "TRUSTED_TOOL_COMPLETION", "tool": tool,
            "tool_call_id": "local-direct-invocation",
            "outcome": "completed_success", "args_hash": digest(envelope["args"]),
            "raw_response_hash": digest(raw),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"kind": "AGENT_TOOL_COMPLETED", "mode": "AGENT_ASSISTED",
                "terminal": True, "attempt_key": envelope["attempt_key"],
                "envelope_hash": envelope["envelope_hash"],
                "response_ref": response_ref,
                "provenance_ref": case.journal.evidence(completion)}

    def test_direct_transport_requires_separate_explicit_review(self):
        with self.assertRaisesRegex(SafetyError, "TRANSPORT_REVIEW"):
            self.case.authorize()
        self.assertFalse(any(row["kind"] == "HANDOFF_DISPATCH_ENABLED"
                             for row in self.case.journal.records()))
        self.enable_direct()
        control = self.case.journal.records()[-1]
        self.assertEqual(control["payload"]["execution_transport"], "DIRECT_GOOGLE_API")

    def test_old_connector_enable_cannot_authorize_direct_preparation(self):
        case = self.case
        connector = ActionBridge(case.journal, "AGENT_ASSISTED",
                                 controller_id="synthetic-controller")
        connector.authorize_agent_dispatch(case.plan, case.approval, case.auth, case.enable)
        with self.assertRaisesRegex(SafetyError, "TRANSPORT_REVIEW"):
            self.bridge.prepare_action(case.plan, case.plan.operations[0].operation_id,
                                       case.approval, case.before, case.check())

    def test_direct_ticket_and_completion_preserve_actual_api_provenance(self):
        case = self.case
        envelope = self.prepare_direct()
        self.assertEqual(envelope["tool"], DIRECT_API_TOOL)
        self.assertEqual(envelope["execution_transport"], "DIRECT_GOOGLE_API")
        ticket = case.mark(envelope)
        self.assertEqual(ticket["tool"], DIRECT_API_TOOL)
        self.assertEqual(ticket["execution_transport"], "DIRECT_GOOGLE_API")
        self.assertEqual(case.journal.records()[-1]["kind"], "ATTEMPTED")
        terminal = self.terminal(envelope)
        observed, parents = case.after()
        result = self.bridge.reconcile_action(
            envelope["envelope_hash"], observed,
            case.check(observed=observed, parents=parents), terminal)
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")
        proof = case.journal.read_evidence(terminal["provenance_ref"])
        self.assertNotEqual(proof["tool"], AGENT_TOOL)

    def test_direct_completion_cannot_claim_assistant_tool_invocation(self):
        case = self.case
        envelope = self.prepare_direct()
        case.mark(envelope)
        terminal = self.terminal(envelope, tool=AGENT_TOOL)
        observed, parents = case.after()
        with self.assertRaisesRegex(SafetyError, "COMPLETION_UNPROVEN"):
            self.bridge.reconcile_action(
                envelope["envelope_hash"], observed,
                case.check(observed=observed, parents=parents), terminal)

    def test_mark_rejects_cross_transport_controller(self):
        envelope = self.prepare_direct()
        case = self.case
        connector = ActionBridge(case.journal, "AGENT_ASSISTED",
                                 controller_id="synthetic-controller")
        with self.assertRaisesRegex(SafetyError, "EXECUTION_TRANSPORT_MISMATCH"):
            connector.mark_attempt(envelope["envelope_hash"], case.before, case.check())
        self.assertFalse(any(row["kind"] == "ATTEMPTED" for row in case.journal.records()))

    def test_fixture_and_disabled_modes_cannot_select_direct_transport(self):
        for mode in ("TEST_ONLY", "DISABLED"):
            with self.subTest(mode=mode), self.assertRaisesRegex(SafetyError, "EVIDENCE_PROFILE"):
                ActionBridge(self.case.journal, mode, execution_transport="DIRECT_GOOGLE_API")

    def test_default_connector_stays_on_original_transport(self):
        case = self.case
        case.bridge = ActionBridge(case.journal, "AGENT_ASSISTED",
                                   controller_id="synthetic-controller")
        envelope = case.prepare()
        ticket = case.mark(envelope)
        self.assertEqual(ticket["tool"], AGENT_TOOL)
        self.assertEqual(ticket["execution_transport"], "AGENT_CONNECTOR")
        terminal = case.completed(envelope)
        observed, parents = case.after()
        result = case.bridge.reconcile_action(
            envelope["envelope_hash"], observed,
            case.check(observed=observed, parents=parents), terminal)
        self.assertEqual(result["kind"], "RECONCILED_AT_TARGET")
