"""Bounded synthetic event-lookup benchmark; no credentials, network or real inventory.

Run from the repository root: PYTHONPATH=. python benchmarks/desired_cache.py
This isolates events() rather than timing Drive or claiming end-to-end throughput.
"""
import json
import signal
import tempfile
import time
from unittest.mock import patch

from uqmc_recovery.desired import DesiredRunner
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import Operation, Plan, canonical


def main():
    def expired(*_):
        raise TimeoutError("Synthetic benchmark exceeded its 10-second bound")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(10)
    try:
        operations = tuple(Operation("synthetic-op-" + str(i), "synthetic-file-" + str(i), "RENAME",
            {"id": "synthetic-file-" + str(i), "name": "Original " + str(i) + ".pdf",
                "mime_type": "application/pdf", "parents": ["synthetic-parent"],
                "synthetic_payload": "fixture-only-" * 350},
            {"name": "Approved " + str(i) + ".pdf"}) for i in range(423))
        plan = Plan("synthetic-run", "synthetic-run", "synthetic-revision", operations)
        sealed_hash = plan.proposal_hash
        plan_bytes = len(canonical(plan.body()).encode("utf-8"))
        with tempfile.TemporaryDirectory(prefix="uqmc-synthetic-cache-") as directory:
            journal = Journal(directory + "/state")
            for index in range(209):
                journal.append("DESIRED_OBSERVED", payload={"plan_sha256": sealed_hash,
                    "source_id": "synthetic-selected" if index % 42 == 0 else "synthetic-other-" + str(index)})
            # No execution: construct only the attributes used by the actual lookup method.
            runner = object.__new__(DesiredRunner)
            runner.journal, runner.plan, runner.plan_sha256 = journal, plan, sealed_hash
            getter = Plan.proposal_hash.fget
            calls = 0
            def counted_hash(value):
                nonlocal calls
                calls += 1
                return getter(value)
            with journal.locked(), patch.object(Plan, "proposal_hash", property(counted_hash)):
                started = time.perf_counter()
                old = [row for row in journal.records() if row["kind"].startswith("DESIRED_")
                    and row["payload"].get("plan_sha256") == plan.proposal_hash
                    and row["payload"].get("source_id") == "synthetic-selected"]
                before_seconds, before_hashes = time.perf_counter() - started, calls
                calls = 0
                started = time.perf_counter()
                new = runner.events("synthetic-selected")
                after_seconds, after_hashes = time.perf_counter() - started, calls
            assert old == new and len(new) == 5
            assert before_hashes == 209 and after_hashes == 0
            print(json.dumps({"synthetic_only": True, "operations": 423,
                "plan_canonical_bytes": plan_bytes, "desired_records": 209, "matching_records": len(new),
                "before_seconds": round(before_seconds, 6), "after_seconds": round(after_seconds, 6),
                "before_hash_evaluations": before_hashes, "after_hash_evaluations": after_hashes,
                "cached_hash_initializations": 1, "results_identical": True}, indent=2))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    main()
