"""Bounded pool proofs using synthetic Drive state and temporary journals only."""
import copy
import threading
import time
import unittest
from unittest.mock import patch, call

import test_desired as fixtures
from test_desired import FakeDrive, ProcessDeath
from uqmc_recovery.desired import DesiredRunner, core
from uqmc_recovery.direct_transport import TransportError
from uqmc_recovery.model import FOLDER, SafetyError


class SharedDrive:
    """Separate thread-owned clients over one locked fake remote server."""
    def __init__(self, files):
        self.drive = FakeDrive(files)
        self.lock = threading.RLock()
        self.clients = []
        self.attempts = []
        self.events = []
        self.active = set()
        self.peak = 0
        self.duplicates = []
        self.permission_calls = 0
        self.before = None
        self.after = None
        self.journal = None

    def client(self):
        client = ThreadClient(self)
        with self.lock:
            self.clients.append(client)
        return client


class ThreadClient:
    apply_enabled = True
    audit_permissions = False

    def __init__(self, server):
        self.server = server
        self.owner = None
        self.used = False

    def touch(self):
        tid = threading.get_ident()
        if self.owner is None:
            self.owner = tid
        if self.owner != tid:
            raise AssertionError("HTTP client shared across worker threads")
        self.used = True

    def get_file(self, file_id):
        self.touch()
        with self.server.lock:
            self.server.events.append(("get", file_id))
            value = self.server.drive.get_file(file_id)
            value.pop("permissions", None)
            return value

    def list_children(self, parent_id):
        self.touch()
        with self.server.lock:
            return self.server.drive.list_children(parent_id)

    def list_permissions(self, file_id):
        with self.server.lock:
            self.server.permission_calls += 1
        raise AssertionError("No-audit mode must not list permissions")

    def update_metadata(self, args, *, etag=None):
        self.touch()
        source_id = args["fileId"]
        with self.server.lock:
            if source_id in self.server.active:
                self.server.duplicates.append(source_id)
                raise AssertionError("Duplicate simultaneous file dispatch")
            self.server.active.add(source_id)
            self.server.peak = max(self.server.peak, len(self.server.active))
            self.server.attempts.append(copy.deepcopy(args))
            self.server.events.append(("update", source_id))
            ordinal = len(self.server.attempts)
        try:
            if self.server.journal is not None:
                intents = [r for r in self.server.journal.records()
                    if r["kind"] == "DESIRED_ATTEMPTED" and r["payload"]["source_id"] == source_id]
                if not intents or intents[-1]["payload"]["args"] != args:
                    raise AssertionError("No durable matching intent before dispatch")
            if self.server.before:
                self.server.before(args, ordinal)
            with self.server.lock:
                value = self.server.drive.update_metadata(args, etag=etag)
                value.pop("permissions", None)
            if self.server.after:
                self.server.after(args, value)
            return value
        finally:
            with self.server.lock:
                self.server.active.remove(source_id)


class DesiredParallelTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DesiredStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.server = None

    def independent(self, count=4):
        operations = list(self.fixture.operations())
        for index in range(1, count):
            source_id = "fixture-extra-" + str(index)
            self.fixture.add_file(source_id, "Original " + str(index) + ".pdf")
            operations.extend(self.fixture.operations(source_id, target_name="Approved " + str(index) + ".pdf"))
        self.fixture.freeze(operations)
        return self.prepare()

    def receiver(self):
        self.fixture.add_file("fixture-receiver", "Old receiver", parent="fixture-parent")
        folder = self.fixture.files["fixture-receiver"]
        folder["mimeType"] = FOLDER
        folder.pop("size", None)
        folder.pop("md5Checksum", None)
        self.fixture.freeze((*self.fixture.operations("fixture-receiver", target_name="Receiver"),
            *self.fixture.operations(target_parent="fixture-receiver")))
        return self.prepare()

    def prepare(self, **options):
        if self.server is None:
            self.server = SharedDrive(self.fixture.transport.files)
            self.server.journal = self.fixture.journal
        return DesiredRunner(self.fixture.journal, self.fixture.plan, self.server.client(),
            audit_permissions=False, retry_delay=0, **options)

    def run_pool(self, runner, **kwargs):
        return runner.run(apply=True, confirm_plan_sha256=self.fixture.plan.proposal_hash,
            workers=kwargs.pop("workers", 3), transport_factory=self.server.client, **kwargs)

    @staticmethod
    def states(result):
        return {item["source_id"]: item["state"] for item in result["items"]}

    def test_bounded_overlap_independent_clients_combined_calls_and_durable_intents(self):
        runner = self.independent(7)
        barrier = threading.Barrier(3, timeout=5)
        def overlap(args, ordinal):
            if ordinal <= 3:
                barrier.wait()
            time.sleep(0.01)
        self.server.before = overlap
        result = self.run_pool(runner)
        self.assertEqual(result["counts"], {"APPLIED": 7})
        self.assertEqual(self.server.peak, 3)
        self.assertEqual(len(self.server.attempts), 7)
        self.assertEqual(len({a["fileId"] for a in self.server.attempts}), 7)
        self.assertEqual(self.server.duplicates, [])
        self.assertEqual(self.server.permission_calls, 0)
        worker_clients = [c for c in self.server.clients[1:] if c.used]
        self.assertEqual(len(worker_clients), 3)
        self.assertEqual(len({c.owner for c in worker_clients}), 3)
        self.assertTrue(all(set(a) == {"fileId", "name", "addParents", "removeParents"}
            for a in self.server.attempts))
        records = self.fixture.journal.records()  # Also validates the complete hash chain.
        self.assertEqual([r["seq"] for r in records], list(range(1, len(records) + 1)))
        self.assertEqual(sum(r["kind"] == "DESIRED_APPLIED" for r in records), 7)

    def test_limit_reserves_inflight_capacity_and_does_not_count_existing_target(self):
        runner = self.independent(6)
        first = runner.items[0]
        self.server.drive.files[first["source_id"]].update(name=first["target"]["name"], parents=first["target"]["parents"])
        result = self.run_pool(runner, limit=2, workers=5)
        self.assertEqual(result["counts"].get("APPLIED"), 2)
        self.assertEqual(result["counts"].get("TARGET"), 1)
        self.assertEqual(len(self.server.attempts), 2)

    def test_duplicate_selection_is_rejected_before_network_or_journal_changes(self):
        runner = self.independent(2)
        before = self.fixture.journal.path.read_bytes()
        with self.assertRaises(SafetyError):
            self.run_pool(runner, source_ids=["fixture-source", "fixture-source"])
        self.assertEqual(self.fixture.journal.path.read_bytes(), before)
        self.assertEqual(self.server.attempts, [])
        self.assertEqual(self.server.events, [])

    def test_folder_is_verified_before_children_and_subset_does_not_expand(self):
        runner = self.receiver()
        blocked = self.run_pool(runner, source_ids=["fixture-source"])
        self.assertEqual(self.states(blocked)["fixture-source"], "DEPENDENCY_BLOCKED")
        self.assertEqual(self.server.attempts, [])
        result = self.run_pool(self.prepare())
        self.assertEqual(result["counts"], {"APPLIED": 2})
        self.assertEqual([a["fileId"] for a in self.server.attempts], ["fixture-receiver", "fixture-source"])
        applied = [r["payload"]["source_id"] for r in self.fixture.journal.records() if r["kind"] == "DESIRED_APPLIED"]
        self.assertEqual(applied, ["fixture-receiver", "fixture-source"])

    def test_partial_failure_and_restart_do_not_repeat_completed_effects(self):
        runner = self.independent(4)
        denied = "fixture-extra-2"
        def deny(args, ordinal):
            if args["fileId"] == denied:
                raise TransportError("DRIVE_HTTP_ERROR", status=403, outcome="UNKNOWN")
        self.server.before = deny
        result = self.run_pool(runner)
        self.assertEqual(result["counts"].get("APPLIED"), 3)
        self.assertEqual(self.states(result)[denied], "NOT_APPLIED")
        previous = [a["fileId"] for a in self.server.attempts]
        self.server.before = None
        result = self.run_pool(self.prepare())
        self.assertEqual(result["counts"].get("APPLIED"), 1)
        self.assertEqual(result["counts"].get("TARGET"), 3)
        self.assertEqual([a["fileId"] for a in self.server.attempts[len(previous):]], [denied])

    def test_lost_response_reconciles_before_retry_and_restart_is_noop(self):
        runner = self.independent(3)
        lost = set()
        def lose_once(args, value):
            if args["fileId"] not in lost:
                lost.add(args["fileId"])
                raise TimeoutError("synthetic response lost after success")
        self.server.after = lose_once
        result = self.run_pool(runner)
        self.assertEqual(result["counts"], {"APPLIED": 3})
        self.assertEqual(len(self.server.attempts), 3)
        self.assertTrue(all(i["historical_request_uncertainty_retained"] for i in result["items"]))
        self.assertEqual(self.run_pool(self.prepare())["counts"], {"TARGET": 3})
        self.assertEqual(len(self.server.attempts), 3)

    def test_cancel_stops_new_scheduling_and_records_inflight_outcomes(self):
        runner = self.independent(6)
        barrier = threading.Barrier(2, timeout=5)
        def cancel_after_both_dispatched(args, ordinal):
            barrier.wait()
            runner.stop_requested = True
        self.server.before = cancel_after_both_dispatched
        result = self.run_pool(runner, workers=2)
        self.assertTrue(result["paused"])
        self.assertEqual(result["counts"], {"APPLIED": 2})
        self.assertEqual(len(self.server.attempts), 2)
        self.assertEqual(self.server.active, set())
        self.assertEqual(sum(r["kind"] == "DESIRED_APPLIED" for r in self.fixture.journal.records()), 2)
        self.server.before = None
        self.assertEqual(self.run_pool(self.prepare())["counts"].get("APPLIED"), 4)

    def test_parallel_rollback_respects_reverse_dependencies_and_restart(self):
        runner = self.receiver()
        self.run_pool(runner)
        initial = len(self.server.attempts)
        first = self.run_pool(self.prepare(), rollback=True, limit=1)
        self.assertEqual(first["counts"].get("ROLLED_BACK"), 1)
        self.assertEqual(self.server.attempts[initial]["fileId"], "fixture-source")
        self.run_pool(self.prepare(), rollback=True)
        self.assertEqual([a["fileId"] for a in self.server.attempts[initial:]], ["fixture-source", "fixture-receiver"])
        final_count = len(self.server.attempts)
        self.run_pool(self.prepare(), rollback=True)
        self.assertEqual(len(self.server.attempts), final_count)
        self.assertEqual(self.server.drive.files["fixture-receiver"]["parents"], ["fixture-parent"])
        self.assertEqual(self.server.drive.files["fixture-source"]["parents"], ["fixture-parent"])
        self.assertEqual(self.server.permission_calls, 0)

    def test_crash_after_remote_success_recovers_without_replaying_update(self):
        runner = self.independent(2)
        crashed = []
        def crash(point, item):
            if point == "after_dispatch" and item["source_id"] == "fixture-source" and not crashed:
                crashed.append(True)
                raise ProcessDeath()
        runner.fault = crash
        try:
            self.run_pool(runner, workers=2)
        except ProcessDeath:
            pass
        self.assertEqual(crashed, [True])
        self.run_pool(self.prepare(), workers=2)
        self.assertEqual(sum(a["fileId"] == "fixture-source" for a in self.server.attempts), 1)
        self.assertEqual(self.server.active, set())
        self.assertEqual(self.server.permission_calls, 0)


    def test_selected_child_proceeds_when_external_receiver_is_already_target(self):
        runner = self.receiver()
        receiver = runner.by_id["fixture-receiver"]
        self.server.drive.files["fixture-receiver"].update(
            name=receiver["target"]["name"], parents=receiver["target"]["parents"])
        result = self.run_pool(runner, source_ids=["fixture-source"])
        self.assertEqual(result["counts"], {"APPLIED": 1})
        self.assertEqual([a["fileId"] for a in self.server.attempts], ["fixture-source"])
        self.assertEqual(self.server.permission_calls, 0)

    def test_prior_rename_then_interrupted_move_preserves_intermediate_checkpoint(self):
        runner = self.independent(1)
        item = runner.by_id["fixture-source"]
        with self.fixture.journal.locked():
            raw, _, _ = runner.observe(item)
            renamed = runner.transition(item, raw, core(raw) | {"name": item["target"]["name"]})
        self.assertEqual(renamed["state"], "APPLIED")
        self.assertEqual(set(self.server.attempts[0]), {"fileId", "name"})
        self.assertEqual(self.server.drive.files["fixture-source"]["parents"], ["fixture-parent"])
        crashed = []
        def crash(point, current):
            if point == "after_dispatch" and not crashed:
                crashed.append(True)
                raise ProcessDeath()
        with self.assertRaises(ProcessDeath):
            self.run_pool(self.prepare(fault=crash))
        self.assertEqual(crashed, [True])
        result = self.run_pool(self.prepare())
        self.assertEqual(result["counts"], {"APPLIED": 1})
        self.assertEqual(len(self.server.attempts), 2)
        self.assertEqual(set(self.server.attempts[1]), {"fileId", "addParents", "removeParents"})
        self.assertEqual(self.server.drive.files["fixture-source"]["parents"], item["target"]["parents"])
        self.assertEqual(self.server.permission_calls, 0)

    def test_rate_limit_403_reads_unchanged_state_before_bounded_jittered_retry(self):
        runner = self.independent(1)
        runner.retry_delay = 2
        def rate_limit_once(args, ordinal):
            if ordinal == 1:
                raise TransportError("DRIVE_RATE_LIMIT", status=403, outcome="UNKNOWN")
        self.server.before = rate_limit_once
        with patch("uqmc_recovery.desired.random.uniform", return_value=1.5) as jitter, \
                patch.object(runner._stop_event, "wait", return_value=False) as delay:
            result = self.run_pool(runner)
            self.assertEqual(result["counts"], {"APPLIED": 1})
            jitter.assert_called_once_with(1.0, 2)
            delay.assert_called_once_with(1.5)
        updates = [i for i, event in enumerate(self.server.events) if event == ("update", "fixture-source")]
        self.assertEqual(len(updates), 2)
        self.assertIn(("get", "fixture-source"), self.server.events[updates[0] + 1:updates[1]])
        records = self.fixture.journal.records()
        attempts = [r["seq"] for r in records if r["kind"] == "DESIRED_ATTEMPTED"]
        between = [r for r in records if attempts[0] < r["seq"] < attempts[1]
            and r["kind"] == "DESIRED_OBSERVED"]
        self.assertEqual(len(between), 1)
        self.assertEqual(between[0]["payload"]["state"], "NOT_APPLIED")
        observed = self.fixture.journal.read_evidence(between[0]["payload"]["observed_ref"])
        self.assertEqual(core(observed), runner.items[0]["original"])
        with patch("uqmc_recovery.desired.random.uniform", side_effect=lambda low, high: high) as jitter, \
                patch.object(runner._stop_event, "wait", return_value=False) as delay:
            for attempt in (1, 2, 3, 20):
                runner.backoff(attempt)
            self.assertEqual(jitter.call_args_list, [call(1.0, 2), call(2.0, 4), call(4.0, 8), call(15.0, 30.0)])
            self.assertEqual(delay.call_args_list, [call(2), call(4), call(8), call(30.0)])
        self.assertEqual(self.server.permission_calls, 0)

if __name__ == "__main__":
    unittest.main()
