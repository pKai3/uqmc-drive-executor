# Refresh the canonical spreadsheet before a new batch

The standalone collector reads the four canonical proposal tabs once per capture using Google Sheets `spreadsheets.get`, with narrow Drive revision reads immediately before and after. It saves exact entered formulas, effective values, displayed values, capture time, version and hashes. A failed read, changed revision, formula error, missing tab or ambiguous mapping blocks preparation. It never substitutes a cached workbook. An unavailable computed result is allowed only in `Rename Coverage!F:F` when its actual header is `Rename proposal`, a nonexecuting display/reference column. Such cells normalize to `null` (unknown), retain the original formula in cell evidence, and appear in `unavailable_formula_cells` with an explicit count. Identity, approval, target name/destination fields and actual formula errors remain strict.

The existing full-Drive token is an accepted scope for this read method. **Enable Google Sheets API in the same Google Cloud project**; no extra OAuth scope or new consent is requested by this code. The 15-second socket timeout and redacted error handling are shared with the Drive transport. This is a socket timeout, not a hard wall-clock cap. Documentation: https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/get

## Pending edits and immutable history

`batch_refresh.refresh_batch(journal, prior_plan, capture_callable, policy, batch_id=...)` validates the prior section authority, fetches once and joins current proposals to verified journal history by stable source ID. Its policy binds the canonical workbook, section, exact approved ID set, and independently verified destination-path-to-ID mappings. Explicit instruction evidence must permit pending name/destination edits; paths alone never supply target IDs.

The saved candidate snapshot separates pending work, unchanged completed work, later proposals for completed files, withdrawals and blockers. An already renamed file is move-only unless the user has authorized a new pending-name edit. A completed/moved file's new proposal remains separate work. No/withdrawn records become durable tombstones. Missing or duplicate mappings, lost approvals, scope additions, unresolved requests and substantive history drift block affected records. Candidate creation is not execution approval and does not rewrite the prior plan, workbook or journal.

## Commands

Private policy and source metadata stay under ignored `private/`. The policy and its sealed instruction/path-binding evidence must be configured for the exact approved section; there is no guessed default.

```sh
.venv/bin/python -m uqmc_recovery.batch_source refresh \
  --store private/state --plan private/prior-plan.json \
  --policy private/refresh-policy.json --token secrets/token-drive.json \
  --batch-id new-section-batch --output private/new-batch-candidates.json
```

Use the existing importer/preflight with fresh source metadata to prepare a new exact Plan from eligible candidates. Its batch ID must match the snapshot. `bind` validates each source, before-state, action and target against that snapshot, rejects history changed since capture, and registers the plan. It does not grant approval or modify Google's data.

```sh
.venv/bin/python -m uqmc_recovery.batch_source bind \
  --store private/state --plan private/new-exact-plan.json \
  --snapshot-ref sha256:THE_REFRESH_EVIDENCE_REFERENCE
```

After the existing exact execution-authority binding is recorded, the normal `desired apply` command can execute. New CLI applies require a recorded refresh/binding captured within five minutes. The plan's names and destinations must exactly match the refreshed candidates. Unknown mappings or unavailable fresh metadata remain preparation blockers rather than implicit permissions. No new authority is inferred from a hash.

`desired resume` may continue only an already started exact immutable run or a fresh bound plan. It skips completed effects through the existing journal/readback workflow. Refreshing does not silently switch the targets of an active run. Rollback retains its separate explicit command and recorded preimages.

## An edit reported during execution

Stop scheduling with Ctrl-C; let in-flight calls finish and reconcile. Then invalidate the old run before preparing a replacement:

```sh
.venv/bin/python -m uqmc_recovery.batch_source invalidate \
  --store private/state --plan private/prior-plan.json \
  --reason 'Owner reported pending proposal edits; refresh before continuing'
```

The single-writer lock rejects invalidation while another process owns the journal. The current process can record the same `BATCH_REFRESH_REQUIRED` notice through its controller; authority checks block subsequent dispatch. Resume of an invalidated plan is blocked. Explicitly refresh/reconcile and freeze a new batch. There is no continuous sheet polling and no claim that unreported remote edits are detected mid-run; the snapshot remains immutable during a run.

All outputs are private local evidence/candidates. This workflow does not publish spreadsheet status or HTML. Offline tests cover capture failures, formulas, version changes, pending edits, completed work, exclusions, section boundaries, stale captures and execution binding. A live Sheets read is still required before calling this integration live-tested.
