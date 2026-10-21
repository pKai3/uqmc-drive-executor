# Local projection interface, version 1

This is the agreed data handoff to the parent thread. No shared workbook, existing HTML viewer, or public hosting is modified by this implementation. There is no configured live publisher. `build_projections(journal, allowed_source_ids=..., identity_basis=...)` returns exact v1 snapshots under `workbook` and `html`, a separate private `operation_log` transport, and separate `diagnostics`. The snapshots and log transport share the same snapshot ID and journal watermark. Transport fields never enter either strict snapshot.

The append-only local journal is authoritative. Spreadsheet checkboxes, proposal decisions, viewer state, and delivery receipts are not execution approvals. The executor requires its own explicit batch approval bound to the frozen proposal revision/hash and operation set.

## Envelope and status contract

Both payloads contain:

- `schema_version`: `uqmc.execution.snapshot.v1`.
- `snapshot_id`: a deterministic UUID for the exact projection pair. Unchanged content and selection retain the ID on retry, including when only one surface previously delivered. Changed content or disclosure selection creates a different ID.
- `generated_at_utc`: ISO 8601 UTC ending in `Z`.
- `journal_sequence` and `journal_tip_sha256`: the latest non-publication journal event. Delivery bookkeeping does not change this watermark.
- `coverage`: `{basis: "tracked_operations_only", last_reconciled_at_utc: null, complete: false, identity_basis_version, identity_count, source_identity_sha256}`. Identity coverage is verified against the explicitly supplied complete identity basis, not inferred from journal operations. This implementation does not infer exhaustive live metadata coverage or a global reconciliation time from individual successful actions.
- `operations`: `operation_id`, `run_id` (equal to batch ID), `source_id`, `action`, `direction`, `state`, `operation_order`, `depends_on_operation_ids`, `approval_state`, `approved_plan_sha256`, `related_forward_operation_id`, `verified_at_utc`, `reason_code`.
- `items`: `source_id`, nullable `source_link`; `move` and `rename`, each containing `effect`, `last_event_state`, `operation_ids`, `verified_at_utc`; `location` containing `effect`, `ancestor_move_operation_ids`, `verified_path`, `path_verified_at_utc`; `observed` containing `exact_name`, `parent_ids`, `mime_type`, `metadata_verified_at_utc`, `read_state`; and `reason_codes`.

Names and paths are unknown unless captured by the relevant verified evidence. No system record means `NOT_TRACKED`, `NEVER_VERIFIED`, and unknown location; it does not prove that a source is still at its original location. Move and rename effects are independent. A later verified failed attempt does not erase an earlier applied effect. An unresolved later dispatch yields `UNKNOWN`; detected live drift yields `CONFLICT`. Successful linked rollback changes only the matching field's effect. A direct move never implies that each descendant itself had a direct move. Verified folder moves with complete unchanged descendant membership produce `INHERITED` location effects for those descendant IDs; linked verified rollback removes that ancestor effect. Incomplete/uncertain ancestor evidence yields `UNKNOWN`. The descendant's own move remains `NOT_TRACKED` unless a distinct direct operation exists. Verified paths remain null; full path verification needs separate evidence.

`approval_state` uses `NOT_APPROVED`, `APPROVED`, or `WITHDRAWN` from journal approval/revocation evidence, and `STALE` when stored approval expiry has passed or cannot be verified. The executor remains responsible for enforcing expiry, revocation, hash mismatch, and batch scope. Unknown reason text is reduced to `REVIEW_REQUIRED`, never forwarded as raw error text. Frozen plan operations without an action event are omitted from `operations`. Their items remain `NOT_TRACKED`, with null last-event state and no fabricated observation. A real `PREPARED` event may establish `UNCHANGED` only when its durable prewrite snapshot supplies the verified baseline. `operation_order` is the 1-based stable dependency order derived from the sealed plan, identical to `Plan.ordered()`, including before dispatch. Raw input order is preserved among simultaneously ready operations; it is not substituted for dependency order. An action without a sealed order blocks export; journal event sequence is never substituted for plan order.

## Existing workbook Operation Log A:Z

The existing 26 headers are preserved exactly. Exported rows contain only `PREPARED`, `ATTEMPTED`, `VERIFIED_APPLIED`, `FAILED_NOT_APPLIED`, `UNCERTAIN`, `BLOCKED_CONFLICT`, or `SKIPPED`. `RECONCILED_AT_TARGET` becomes `VERIFIED_APPLIED` with `OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN`, which explicitly avoids a claim of causation or exactly-once execution. Policy events such as approvals, drafts, exclusions and revocations do not become Operation Log rows.

Each row is keyed by its immutable `Event ID`. A future workbook sink must read existing IDs, append missing IDs only, use `valueInputOption=RAW` (or explicit `userEnteredValue.stringValue`) for every cell, and read back the new rows before acknowledging delivery. Exact names beginning with `=`, `+`, `-`, or `@` must remain literal strings; do not add an apostrophe to the actual title. Parent arrays are JSON strings. Blank means unknown/not captured, never an empty verified parent set.

The separate private `operation_log` transport includes exact names, parent IDs and MIME from evidence, plus opaque `sha256:` evidence references. It preserves `sheet_name`, `valueInputOption=RAW`, `idempotency_column`, `headers` and `rows` outside the strict snapshots. It contains no permission roster or raw errors. Do not serve this export or the local evidence directory from the HTML viewer. Operation order is the 1-based stable dependency order of the frozen plan, including for `PREPARED` events. Full journal sequencing and every attempt remain available in the journal.

## Redacted HTML selection

The HTML payload uses a strict field allowlist. Public export requires an explicit `allowed_source_ids` selection; the default selection is empty. IDs are retained exactly for joins with the parent's existing proposal viewer. This is an explicit disclosure selection, not a permission change. Select only records already cleared for that viewer. Permanent journal exclusions are removed from the public selection even if accidentally included in the allowlist. No source metadata is fetched by this module.

The public payload has no names, parent arrays, MIME values, verified paths, actor names, permission data, private evidence references, file paths, raw request/response data, or raw error strings. `source_link` defaults to null; it is populated only from an explicit `verified_source_links` map of links already approved for the existing viewer. No link is generated from a source ID. The observed fields requiring those values are null. It contains only selected source IDs, operation/status fields, hashes, timestamps and safe reason enums. Dependencies and rollback links are restricted to selected operation IDs. The same snapshot ID allows the parent to detect workbook/viewer mismatch; all future UI rendering must use safe text rendering rather than HTML interpolation.

## Delivery is separate from action success

`deliver_projection(journal, destination, payload, sink)` accepts `workbook`, `html`, or `operation_log` destinations and records `PUBLICATION_QUEUED`, `PUBLICATION_FAILED`, or `PUBLICATION_SUCCEEDED`. Before queueing it retains the exact projection as private evidence and records its `projection_ref`, so crash recovery can reload the same payload. That reference never enters the HTML payload. It has no live sink and never invokes a Drive mutation. Sink errors are reduced to `DELIVERY_FAILED`; they do not alter operation states. The `delivery_key` is stable across regeneration of the same source snapshot, and workbook sinks must additionally deduplicate `Event ID`.

The sink must return `{verified: true}` only after readback confirms the intended payload. A timeout after remote delivery is an uncertain delivery: retry with the same key and inspect remote event IDs/version first. A crash after delivery but before journal acknowledgment may cause a repeated sink invocation; exactly-once delivery is not promised. Existing successful delivery keys are skipped. Publication is not authorized for the present task.

## Parent integration requirements

1. Preserve the exact A:Z headers and use literal cell writes.
2. Agree the selected source ID allowlist and snapshot attachment location before publication.
3. Add status views from these fields without presenting untracked records as unchanged.
4. Display the snapshot timestamp, journal watermark and coverage limit. Local `diagnostics.stale` is null until compared with a separately observed journal watermark; `diagnostics.freshness` is `SNAPSHOT_ONLY`, `STALE`, or `JOURNAL_CURRENT`. These transport diagnostics are not fields of the shared schema and are not proof of current live Drive state.
5. Use the same snapshot pair for workbook and HTML; report either delivery failure separately.
6. Keep `private/`, journal records, backups and private workbook exports outside the document root and any public bundle.

The interface is ready for local integration review. The shared workbook and HTML viewer have not been changed.

## Identity basis and exact validation

The versioned contract is `schemas/uqmc.execution.snapshot.v1.schema.json`. `validate_snapshot` checks exact object keys, nullable operation source/reason fields, nonempty required identifiers, unique arrays, types, bounds, UTC timestamps, UUID and status enums without third-party dependencies, and runs on both snapshots before return. Extra transport/evidence fields are rejected at every nested object level.

Supply `identity_basis={"identity_basis_version": positive_integer, "source_ids": complete_active_ID_list, "identity_count": expected_count, "source_identity_sha256": expected_sha256}`. The fingerprint is SHA-256 of UTF-8 `"\n".join(sorted(source_ids))` with no trailing newline. Duplicates, empty IDs, IDs containing line breaks, mismatched count/hash, and disclosure selections outside that basis fail closed. The basis version/count/hash are copied into coverage; the complete private list is never included in a public payload. Python API fixtures may omit the basis and derive version 1 from the explicit local export selection; diagnostics label that `LOCAL_EXPORT_SELECTION`. Real CLI export requires a supplied basis file.

```sh
python3 -m uqmc_recovery --private-dir private export \
  --identity-basis private/viewer-identity-basis.json \
  --allowed-source-ids private/viewer-allowed-source-ids.json
```

The command writes four private local files: exact workbook snapshot, redacted HTML snapshot, Operation Log transport, and diagnostics. It performs no network calls or publication. Omitting the optional allowlist keeps the HTML item and operation arrays empty. No basis, inventory, log, or evidence file belongs in Git or a public document root.
