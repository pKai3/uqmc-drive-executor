# Validation evidence

Validated on Python 3.14.7 on macOS, 2026-10-21.

`python3 -m unittest discover -s tests -v` passed **103 tests**:

| Area | Tests |
| --- | ---: |
| Durable journal, corruption, locking, evidence and backups | 17 |
| Agent-assisted read request/receipt reconciliation | 15 |
| Execution/rollback engine and injected crash boundaries | 32 |
| Workbook import, exact targets, exclusions and stale inputs | 20 |
| Private/public projections and independent delivery recovery | 19 |

Engine tests exercise 28 injected crash boundaries, including before/after action-state transitions, remote success before commit, failed readback, reconciliation and postflight. They cover delayed completion, transitive dependencies, partial success, explicit access-change approval, manual drift, stale/revoked approval, retries and repeated rollback.

The runnable CLI demo produced: MOVE `VERIFIED_APPLIED`, RENAME `UNCERTAIN`; resume reconciled the rename at target without another dispatch; two linked rollback actions restored the original exact name and parent. Repeating rollback left total mock dispatches at four. Original folders remained. All remote objects in this test were synthetic in-memory fixtures.

A separate CLI smoke run executed `init`, `prepare`, `receipt` and `reconcile` against synthetic metadata. It returned `READ_ONLY_METADATA_COMPLETE` with `execution_ready=false` and contained only read events, no action or approval events. A baseline backup and its private manifest were created and read back locally.

Read-only live validation inspected only the supplied workbook's metadata, Operation Log/Rollback Guide and proposal headers. It did not reopen the permanently withdrawn source, read source content, perform any club move/rename/delete/permission change or publish a viewer. The connector omissions and future pilot gates are documented in [operations.md](operations.md). Live execution and live projection delivery remain unimplemented/disabled.

These are fixture and local durability results, not certification of a real connector mutation, power-loss durability of hardware, complete effective access or exactly-once delivery.
