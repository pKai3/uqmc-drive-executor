# UQMC Drive executor and recovery journal

Local safety infrastructure for reviewed, partial Drive reorganisations. **Live mutation is disabled.** This repository contains no Drive credentials and cannot invoke an assistant's connected tools from Python. Current runnable capabilities are durable storage, spreadsheet import, read-only agent request/receipt reconciliation, status exports, and a fixture executor with rollback and failure injection. A successful simulation is not a live pilot.

## Offline duplicate review

The independent [deduplication report CLI](docs/deduplication.md) compares exported
metadata and optional supplied hashes without contacting Drive or reading document
bodies. It separates trusted binary checksum matches, name/content candidates and
identity references, requires a private exclusion policy, and writes private JSON
and CSV reports. Native-file exports never establish full equivalence; every
finding requires manual review and preserves distinct version and usage context.

```sh
python3 -m uqmc_recovery.dedup --inventory examples/dedup/inventory.example.json --ignore-config examples/dedup/ignore.example.json --hashes examples/dedup/hashes.example.json --output-dir private/dedup-demo
```

This example uses synthetic data only. Use a new output directory for each run.

## Run locally

Python 3.11+ and the standard library are sufficient; no installation or network is required.

```sh
python3 -m unittest discover -s tests -v
python3 -m uqmc_recovery init
python3 -m uqmc_recovery status
python3 -m uqmc_recovery demo
python3 -m uqmc_recovery export
python3 -m uqmc_recovery backup private/backups/checkpoint-001
```

`demo` uses only synthetic in-memory files. It simulates a successful move, a rename whose response is lost, reconciliation, separate inverse operations, and repeat-safe rollback. It stores its evidence separately from the real empty execution journal. No proposal checkbox or configuration command grants execution authority.

`private/` and generated projections are ignored before staging. This folder contains the actual workbook ID, permanent withdrawal policy, connector receipts, exact metadata, permission snapshots and journals. Do not stage ignored files with `git add -f`. Exported status and proposal inputs can still contain private data. Neither the repository nor any public viewer is an evidence backup.

## Spreadsheet input and approvals

See [importer contract](docs/importer.md). The importer reads complete connector-exported proposal ranges, exact headers and explicit selection. `Yes` makes a row eligible for review; it does not authorise execution. Paths and display labels are never used as Drive mutation targets. Exact destination IDs, complete current metadata, and reviewed exact native titles are required. Source and row withdrawals persist across revisions. A changed workbook fingerprint invalidates target resolutions and approval binding.

The frozen plan binds the batch ID (also Run ID), proposal revision/hash, stable operation IDs, source IDs, exact field-only targets, dependencies and exclusions. Future human approval must name that exact batch and hash, scope, actor, expiry and any reviewed access changes. A hash authenticates no person. The current approval class accepts only fixture TEST_ONLY approvals and the dispatcher accepts only the in-memory MockAdapter. There is no live-enable CLI flag.

## Agent-assisted read workflow

```sh
python3 -m uqmc_recovery prepare --plan private/candidate-plan.json
python3 -m uqmc_recovery receipt --request-id REQUEST --file-id FILE --input private/metadata-receipt.json
python3 -m uqmc_recovery reconcile --request-id REQUEST
```

`prepare` produces exact read-only tool requests for an assistant to perform using already-authorised connectors. It checks withdrawal policy before issuing any request. The assistant saves the observed connector result to the private receipt file. `receipt` binds that result to the frozen request and stable file ID; `reconcile` reports omissions and readiness. These commands do not perform a remote action and cannot import a claimed mutation success. A future mutation bridge needs explicit implementation and approval; it is not silently provided by these commands.

Observed connector limits and the pilot gates are in [operations and recovery](docs/operations.md). The standalone adapter deliberately raises an error rather than pretending it can access authenticated tools.

## Evidence and recovery

Events are append-only, canonical JSON with a SHA-256 chain, UUID event IDs, monotonic sequence and UTC time. Evidence is content-addressed, atomically installed, fsynced and read back. A process/thread lock serializes the full batch. Directories use `0700`; files use `0600`. Hashes detect accidental changes, but a local administrator can rewrite both history and hashes. This is not signed, hardware-immutable or exactly-once storage.

Every simulated action records full preimage, intended state, effective-access preflight and approval reference before `ATTEMPTED`. It re-reads before dispatch, checks a conditional write in the fixture, reads back afterward and records verification separately. The actual connector currently exposes neither the fixture's atomic conditional check nor a reliable request-terminal receipt; therefore it cannot satisfy this execution contract yet.

A timeout, interrupted dispatch, or missing commit stays uncertain. Reconciliation reads the same stable ID. Observing the desired state is recorded with attribution uncertainty. Observing the old state alone never permits retry while the original request might complete later. A new batch cannot bypass an unresolved earlier request. Verified failed attempts need a newly reviewed batch; resume never expands scope or blindly retries.

Rollback is a new separately approved plan linked to verified forward operations. It follows reverse dependencies and successful issue order, compares current state to recorded postimages, restores only the approved name or parents, and blocks manual drift. Folder membership and access are preflight conditions. Original folders are retained. There are no delete, permission-edit, create-folder or content-write actions. Moving back cannot undo information exposure.

The journal is authoritative; workbook and HTML are projections. Publication failures remain separate from action outcome. See [sync interface](docs/sync-interface.md) for the agreed `uqmc.execution.snapshot.v1` contract and unchanged A:Z Operation Log. Public output defaults to no source IDs and requires an explicit allowlist. It never contains raw evidence or permission baselines. No server is started and nothing is published.
