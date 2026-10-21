# Workbook importer

`uqmc_recovery.importer` converts an explicitly selected set of workbook proposals into a frozen candidate `Plan`. It never grants approval and has no network client. A `Yes` decision only makes a proposal eligible for review. The configuration request, a checkbox, a plan hash, and an imported resolution are not batch execution authority.

The implementation uses the actual column headers verified by a read-only, header-only connector inspection on 2026-10-21. No source files were opened for this inspection. Live workbook IDs, exports, inventories, source metadata and exclusion identities belong only in ignored private configuration/evidence. The repository's default workbook identity is `fixture-workbook`; a real run must pass `expected_workbook_id` from independently configured private policy. The importer rejects a different workbook.

## Input boundary

Pass a connector-exported object with these fields:

```json
{
  "workbook_id": "fixture-workbook",
  "exported_at_utc": "2026-10-21T00:00:00Z",
  "complete": {
    "Proposed Moves": true,
    "Move Manifest": true,
    "Proposed Renames": true,
    "Rename Coverage": true
  },
  "sheets": {
    "Proposed Moves": [["...exact headers..."], ["...values..."]],
    "Move Manifest": [["...exact headers..."], ["...values..."]],
    "Proposed Renames": [["...exact headers..."], ["...values..."]],
    "Rename Coverage": [["...exact headers..."], ["...values..."]]
  }
}
```

The export collector must establish full coverage of these source tabs, including excluded rows. A bounded or capped read cannot be labeled complete. The importer fails closed on missing tabs, incomplete exports, missing or duplicate headers, duplicate references and malformed rows. It does not discover additional files or treat paths as identities.

Move references are `Proposal ID/Manifest item`, joining `Proposed Moves` to `Move Manifest`. `Move Manifest.Source ID` supplies the stable source ID. Rename references are `Proposed Renames.Rename ID`, with `Source ID` from that row. `selected_refs` must explicitly enumerate the intended records; an empty selection never means all rows.

`metadata_by_id` contains complete normalized source and parent snapshots from already authorized reads. The importer reads this dictionary only after evaluating exclusions. Unknown version, missing permission baseline, incomplete effective access, unavailable original parents and incomplete folder descendants block the candidate. Execution still requires a fresh adapter preflight and read immediately before dispatch; imported metadata is not proof that Drive remains unchanged.

The current workbook includes destination paths and display labels, not sufficient machine-executable target IDs or exact native titles. Supply a separately reviewed `resolutions` entry per selected reference:

```json
{
  "input_sha256": "hash from catalog(export)",
  "row_sha256": "hash for the exact joined proposal record",
  "source_id": "fixture-source",
  "live_snapshot_sha256": "hash of the complete source snapshot",
  "depends_on_refs": [],
  "reviewed_checks": true,
  "target_parent_ids": ["fixture-destination"]
}
```

For rename resolutions, replace `target_parent_ids` with `exact_target_name`. The resolution must bind the exact workbook and live snapshot hashes. It is not a signature and does not authenticate an approver. It documents a target resolution for the later concrete batch review. Names with `.gdoc`, `.gsheet` or `.gslides` display suffixes are never copied or automatically stripped. The exact title must be resolved explicitly; the model rejects adding such a suffix to a native title and preserves uploaded-file extensions.

Dependencies must be reviewed explicitly as reference IDs. Free-text dependency and exception prose is retained in the evidence; it is not executed or interpreted as code. Multiple changes to the same source need an explicit order. The importer produces separate operations and uses the preceding intended state as the next baseline. Folder/descendant overlap still requires a valid dependency relationship and complete exclusion/access preflight.

## Freeze and stale checks

```python
from uqmc_recovery.importer import catalog, import_workbook, assert_frozen_input

inventory = catalog(export, expected_workbook_id=private_policy["workbook_id"])
result = import_workbook(
    journal, export,
    expected_workbook_id=private_policy["workbook_id"],
    batch_id="review-batch-001",
    selected_refs=["MOVE-001/ITEM-001"],
    metadata_by_id=verified_metadata,
    resolutions=reviewed_resolutions,
)
assert result.manifest["execution_approved"] is False
# Re-read the complete workbook input before separately approving the batch.
assert_frozen_input(result.manifest, fresh_export)
```

`input_sha256` hashes the exact workbook identity, matrices and completeness declarations. Fetch time is excluded from that content hash, so a later identical read remains identical. `proposal_revision` is derived from the workbook input hash. `Plan.proposal_hash` additionally binds the exact selection, source snapshots, targets and dependency graph. Operation IDs are stable for the same batch, workbook snapshot, reference and action. A different batch deliberately produces new operation IDs.

An import with any selected blocked row returns `plan=None`; it never silently drops that row to authorize a smaller batch. The dry-run manifest reports each reason. A valid plan remains `ELIGIBLE_PENDING_BATCH_APPROVAL` and `execution_approved=false`. The caller must present the concrete plan and obtain the separate explicit batch approval. The executor must not continuously follow editable cells once a batch is frozen. Workbook changes or withdrawals require revalidation, never implicit scope expansion. `assert_frozen_input` rejects changed content. `expected_input_sha256` can also reject stale reimports.

## Exclusion persistence

Explicit file-level `Withdrawn`/`Excluded` markers create durable `EXCLUDED` events; the hard denylist also applies before metadata access. A `No` decision creates a durable `PROPOSAL_EXCLUDED` reference event. A rejected rename consequently cannot be reintroduced by changing its cell to `Yes`, while a separately approved move of that same source remains independently reviewable. A declined move proposal excludes its current manifest records and the parent proposal reference, so later-added manifest rows cannot evade it. The engine must enforce both file and proposal-reference tombstones. No automatic unexclude operation is provided.

An exact `Excluded by user` display marker with every other cell empty is a fully redacted tombstone, reported by tab and row without attempting to recover its identity. A marker retaining complete proposal references may instead be quarantined when all source IDs, source paths and target fields are empty. The one permitted target-column placeholder is exactly `Pending restricted-destination review` in a quarantined rename's `Proposed destination folder`; it identifies no destination and is never used for an operation. Any other nonempty target, source identity, incomplete reference, or unknown populated column still blocks import. Historical review text is inert. Quarantine excludes the parent proposal ID for a Proposed Moves row, the exact proposal/manifest reference for a Move Manifest row, or the exact rename ID for a Proposed Renames row. These references are persisted before metadata access and remain excluded if later worksheet edits restore identities or decisions. Both tombstone forms remain in the raw workbook fingerprint and private evidence; neither yields an executable catalog record.

The importer saves the raw input, resolutions, manifest and optional candidate plan in private content-addressed evidence and appends an `IMPORTED` bookkeeping event. It never appends `APPROVED`, `PREPARED`, `ATTEMPTED` or action-success events. Import output does not prove anything moved or remained at its original location.

## Verification and remaining integration

`python3 -m unittest discover -s tests -p test_importer.py -v` exercises the workbook-shaped fixtures, proposal joins, explicit scope, stable IDs, stale edits/resolutions, missing metadata, denied records, source exclusions, forbidden descendants, native titles, uploaded extensions, and move/rename dependency chains. All fixture identities are synthetic.

The installed connector is session-callable; this module cannot invoke it from standalone Python. A future authorized collector must pass complete authenticated range exports and complete metadata into this interface. The present header-only read is not a full live inventory import, and no actual workbook decisions have been converted into execution approval. The module does not write the workbook, HTML, source files or permissions.
