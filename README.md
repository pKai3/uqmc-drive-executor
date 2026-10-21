# UQMC Drive executor and recovery journal

Local safety infrastructure for reviewed, partial Drive reorganisations. **Live mutation is disabled.** This repository contains no Drive credentials and cannot invoke an assistant's connected tools from Python. Runnable capabilities include durable storage, spreadsheet and destination-section import, read receipts, a guarded agent handoff, status exports, and a fixture executor with rollback and failure injection. The agent handoff prepares and records a separately invoked assistant tool call; it does not connect Python to Drive. A successful simulation is not a live pilot.

## Run locally

Python 3.11+ and the standard library are sufficient; no installation or network is required.

```sh
python3 -m unittest discover -s tests -v
python3 -m uqmc_recovery init
python3 -m uqmc_recovery status
python3 -m uqmc_recovery demo
python3 -m uqmc_recovery handoff-demo
python3 -m uqmc_recovery export
python3 -m uqmc_recovery backup private/backups/checkpoint-001
```

`demo` uses only synthetic in-memory files. It simulates a successful move, a rename whose response is lost, reconciliation, separate inverse operations, and repeat-safe rollback. It stores its evidence separately from the real empty execution journal. No proposal checkbox or configuration command grants execution authority.

`private/` and generated projections are ignored before staging. This folder contains the actual workbook ID, permanent withdrawal policy, connector receipts, exact metadata, permission snapshots and journals. Do not stage ignored files with `git add -f`. Exported status and proposal inputs can still contain private data. Neither the repository nor any public viewer is an evidence backup.

## Spreadsheet input and approvals

See [importer contract](docs/importer.md). The importer reads complete connector-exported proposal ranges, exact headers and explicit selection. `Yes` makes a row eligible for review; it does not authorise execution. Paths and display labels are never used as Drive mutation targets. Exact destination IDs, complete current metadata, and reviewed exact native titles are required. Source and row withdrawals persist across revisions. A changed workbook fingerprint invalidates target resolutions and approval binding.

The frozen plan binds the batch ID (also Run ID), proposal revision/hash, stable operation IDs, source IDs, exact field-only targets, dependencies and exclusions. A destination-section approval can be resolved into that exact operation set without requiring per-row ticks or a second human approval of a technical hash. The importer records the original approval reference and independently reviewed section mapping; later added IDs or changed mappings require revalidation. A hash binds evidence but authenticates no person. See [section approval](docs/section-approval.md). Standalone dispatch accepts only the in-memory MockAdapter; agent handoff requires separate reviewed local enablement and a single controller.

## Agent-assisted read workflow

```sh
python3 -m uqmc_recovery prepare --plan private/candidate-plan.json
python3 -m uqmc_recovery receipt --request-id REQUEST --file-id FILE --input private/metadata-receipt.json
python3 -m uqmc_recovery reconcile --request-id REQUEST
```

`prepare` produces exact read-only tool requests for an assistant to perform using already-authorised connectors. It checks withdrawal policy before issuing any request. The assistant saves the observed connector result to the private receipt file. `receipt` binds that result to the frozen request and stable file ID; `reconcile` reports omissions and readiness. These commands do not perform a remote action and cannot import a claimed mutation success. These read-only commands do not authorize the separate action handoff.

The [action handoff protocol](docs/action-handoff.md) adds `prepare-action`, `mark-attempt`, `ingest-result` and `reconcile-action`. The default is disabled. Its agent mode requires a frozen approval, independently reviewed dispatch enablement and fresh evidence; actual connector integration remains subject to review and a bounded pilot. `handoff-demo` bootstraps a fake-tool example and proves the local ticket cannot be issued twice. A local ticket cannot prevent a person from manually repeating an external tool call, and it is not an atomic Drive precondition or server idempotency key.

Observed connector limits and the pilot gates are in [operations and recovery](docs/operations.md). The standalone adapter deliberately raises an error rather than pretending it can access authenticated tools.

## Evidence and recovery

Events are append-only, canonical JSON with a SHA-256 chain, UUID event IDs, monotonic sequence and UTC time. Evidence is content-addressed, atomically installed, fsynced and read back. A process/thread lock serializes the full batch. Directories use `0700`; files use `0600`. Hashes detect accidental changes, but a local administrator can rewrite both history and hashes. This is not signed, hardware-immutable or exactly-once storage.

Every simulated action records full preimage, intended state, effective-access preflight and approval reference before `ATTEMPTED`. It re-reads before dispatch, checks a conditional write in the fixture, reads back afterward and records verification separately. The actual connector exposes no atomic conditional check. Agent mode records that limitation, serializes this controller and requires fresh reads, while explicitly retaining the race between the last read and the remote write. It does not invent optional metadata or claim an atomic transaction. Unknown outcomes remain fenced unless explicit terminal evidence supports reconciliation.

A timeout, interrupted dispatch, or missing commit stays uncertain. Reconciliation reads the same stable ID. Observing the desired state is recorded with attribution uncertainty. Observing the old state alone never permits retry while the original request might complete later. A new batch cannot bypass an unresolved earlier request. Verified failed attempts need a newly reviewed batch; resume never expands scope or blindly retries.

Rollback is a new separately approved plan linked to verified forward operations. It follows reverse dependencies and successful issue order, compares current state to recorded postimages, restores only the approved name or parents, and blocks manual drift. Folder membership and access are preflight conditions. Original folders are retained. There are no delete, permission-edit, create-folder or content-write actions. New-folder mode blocks until separately authorized creation has a verified receipt; an uncertain creation is never retried by name. Reuse mode requires explicit identity provenance. Approved duplicate names are preserved by exact peer IDs; unseen collision IDs still block. Moving back cannot undo information exposure.

The journal is authoritative; workbook and HTML are projections. Publication failures remain separate from action outcome. See [sync interface](docs/sync-interface.md) for the agreed `uqmc.execution.snapshot.v1` contract and unchanged A:Z Operation Log. Public output defaults to no source IDs and requires an explicit allowlist. It never contains raw evidence or permission baselines. No server is started and nothing is published.
