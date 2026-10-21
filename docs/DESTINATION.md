# Run one destination section

From the repository, use the existing Python environment and private full-Drive
token. The command takes the canonical destination path exactly as it appears in
the proposal spreadsheet. It does not need an assistant to construct a batch or
write per-file mappings.

Quote the exact canonical destination path, preserving any spaces. File titles
are opaque: literal slashes and trailing spaces are retained. The planner binds
a file's parent using its stable ID, exact title/MIME and structured destination
fields; it never creates folders from slash fragments inside a title. Missing or
conflicting title-boundary evidence stops with an explicit error.

```sh
cd /Users/bcsinger/Documents/GitHub/uqmc-drive-executor
.venv/bin/python -m uqmc_recovery.destination '06 Trip Leaders'
```

Console output is readable by default: stage, elapsed time, progress counts,
verified filenames, errors, and a separate publication result. Metadata progress
is summarized every ten items; execution prints one completion line per item.
The complete JSON progress and final result are also saved to a private, flushed
and synced JSONL transcript under the configured run directory's `console-logs/`.
The execution journal remains the authority for recovery. Source/destination
checks, pre-plan backup, plan/authority preparation, approved-batch backup, final
recovery backup and result exports each report start/completion and duration.
The final runner backup is reused as the saved checkpoint after its journal head
and bytes are checked; the command does not immediately copy that store again.
Each backup still validates and independently copies all evidence with fsync and
readback, but repeated references do not cause repeated parsing of the same blob.
Use `--json` for JSON
records on stdout, for example:

```sh
.venv/bin/python -m uqmc_recovery.destination '06 Trip Leaders' --json
```

The default is a read-only preview. It reads the fresh spreadsheet and relevant
Drive metadata, checks the authenticated account and reports pending, complete
and blocked items. It does not migrate files or publish status.

Successful preview metadata reads and folder listings are saved incrementally
under the private run directory, with per-read timestamps and content hashes.
A repeated preview may reuse those reads for up to five minutes, only for the
same section, fresh proposal fingerprint and journal sequence. A timeout retains
completed reads; retry requests the missing reads instead of repeating the whole
scan. Changed input, expired entries or changed journal history invalidate reuse.
The summary reports how many cached reads were available. Preview observations
can therefore be up to five minutes old; they are not execution preconditions.

Apply never consumes this preview cache. Pending items receive fresh metadata,
capabilities, collision checks and the runner's pre-write checks. Already verified
completed effects may be skipped using durable history, explicitly reported as
saved history rather than a new live verification. Transient collection reads
have at most two attempts; a permanent permission-denied 403 is not retried.
Failures include the method/item context and do not authorize another write.

Every mutation source still requires exact own parent IDs. An unmutated boundary
folder may omit its own ancestors in Drive's response; that stays explicitly
unknown in private evidence, never an invented empty parent list. The command
resolves the required relationship from the selected child's exact parent ID
and the parent's complete direct listing. Wrong identity, name/MIME disagreement,
missing membership, invalid capabilities or a trashed parent still block. Known
destination placement and every folder being moved retain strict parent checks.

To execute that destination section and publish its verified status:

```sh
.venv/bin/python -m uqmc_recovery.destination '06 Trip Leaders/Skills and Assessment' --apply
```

`--apply` is explicit authority for that selected destination subtree only. The
command does not widen its scope to other items sharing a source folder. Existing
completed sections and proposal/approval cells are preserved.

## Whole-subtree coverage

New batches include eligible `Keep name` entries from Rename Coverage when their
stable IDs and exact destination paths bind uniquely to an enclosing whole-subtree
Move Manifest row. They become move-only operations; no rename is synthesized.
Explicit per-item routes, exclusions, and verified completion history take
precedence. Only the requested destination subtree is expanded. Historical saved
plans retain their original importer version and are not silently enlarged.

A live child absent from both the proposal inventory and explicit routes stops
with `WHOLE_SUBTREE_CHILD_MISSING_PROPOSAL_COVERAGE`; refresh its inventory mapping
before starting a new batch. The command never guesses where a newly discovered
identity belongs or silently counts an omitted child as completed.

## One-time local configuration

Keep project settings in ignored `private/destination-config.json`: the canonical
workbook, existing journal, existing token, destination root, source exclusions,
and established publication locations. This is project configuration, not a new
batch mapping. The command derives each batch's exact IDs, names, destinations
and dependencies from the fresh sheet and verified history. Keep configuration,
inventory, journals, backups and tokens out of Git and public HTML.

The existing checkout is configured with these keys. Keep the real IDs/account
in the private file; the bracketed entries below describe existing values and
are not literal IDs to paste:

```json
{
  "workbook_id": "<canonical proposal workbook ID>",
  "expected_account": "<existing authorized account email>",
  "token": "secrets/token-drive.json",
  "root_id": "<verified project destination root ID>",
  "anchor_paths": {"06 Trip Leaders": "<verified existing parent folder ID>"},
  "excluded_ids": ["<existing permanently excluded IDs>"],
  "store": "private/state",
  "registry": "private/destination-registry.json",
  "run_directory": "private/destination-runs",
  "backup_directory": "private/backups",
  "publication_directory": "private/destination-publications",
  "identity_basis": "private/identity-basis.json",
  "viewer_file_id": "<original Drive HTML file ID>",
  "accepted_inherited_access": true,
  "workers": 10,
  "revision_check_seconds": 30
}
```

The registry preserves prior run IDs and verified destination IDs. Do not replace
it, the identity basis, exclusions or journal with empty files. Project anchors
are exact known folder IDs; a display label is not proof of source-folder reuse.
Paths are relative to the current working directory, so run from the repository.
Use `--config PATH` only when selecting another explicitly prepared project
configuration. The account returned by Drive must match `expected_account`.

Use the existing `secrets/token-drive.json` token. Drive and Sheets APIs must be
enabled in its Google Cloud project. The command does not initiate OAuth, replace
credentials, request additional scopes or assume a particular Google account.
Permission auditing is disabled under the existing section exposure policy;
permissions are never edited. Identity, name, parent, content metadata and needed
capability checks remain active.

## Fresh plan and recovery

A new batch fetches the canonical spreadsheet once for planning, records its revision, capture
time and hash, and freezes the exact selected section. It does not read a full
sheet for each file or change the frozen plan in response to editable cells.
A lightweight remote revision guard checks at most once per configured interval
(default 30 seconds) during progress and stops new dispatch when it detects a
changed or unavailable revision. Edits can occur between these periodic checks;
the guard is not an atomic lock on the sheet. Already dispatched requests finish readback and journal their outcomes;
then refresh/reconcile the affected work before continuing.

Completed effects are joined by stable file ID and skipped. If a later proposal
changes a completed name or destination, the command reports separate proposed
work instead of silently executing it. Pending changes may adopt the latest
approved mapping within the selected section. Missing, withdrawn or ambiguous
mappings stop affected work; an unavailable refresh is not permission to reuse a
stale local plan.

Intact source folders can move where their complete assigned contents fit the
destination structure. Mixed source folders stay in place while only assigned
items are extracted. Missing required destinations are created with a durable
logical key and recorded Drive ID. An uncertain create is reconciled by that key;
an ambiguous match stops instead of blindly creating another folder.

Resume the latest saved run for the same destination:

```sh
.venv/bin/python -m uqmc_recovery.destination '06 Trip Leaders/Skills and Assessment' --resume --apply
```

`--resume` is a flag, not a run ID. It selects the latest registered run whose
canonical destination matches. Without `--apply` it only reports that saved run.
An execution-relevant proposal change blocks resume; reconcile the old requests
before starting a fresh batch. Resume rechecks current state and preserves the
frozen history. It skips verified
completed effects, reconciles interrupted calls and never treats a timeout as
proof that no write happened. A known original name is a known current state,
even while the historical request outcome remains uncertain.

Rollback is explicit and never automatic. It restores the recorded before name
and parents only while current state matches the recorded applied state. A manual
change produces a conflict. Use the exact run ID printed in the result:

```sh
.venv/bin/python -m uqmc_recovery.destination '06 Trip Leaders/Skills and Assessment' --rollback RUN_ID --apply
```

Rollback uses reverse dependency order, leaves old source containers intact and
does not promise to restore inherited access. It reverses transitions recorded
for that run, not all historical work on the section. A prior rename preserved
by a later move-only run therefore stays renamed when that later run rolls back.
New destination folders are not deleted.

## Publication recovery

Action results are durable before publication. A spreadsheet or HTML publication
failure does not make a verified migration fail and must not cause another file
move. Use `--publish-only DIRECTORY --apply` with the saved publication directory
to retry tracking delivery without migrating files:

```sh
.venv/bin/python -m uqmc_recovery.destination '06 Trip Leaders/Skills and Assessment' --publish-only private/destination-publications/DELIVERY_DIRECTORY --apply
```

Use the actual delivery directory reported by the failed publication. This mode
requires `--apply` and does not execute a migration or rollback. Status is joined
by source ID; event IDs deduplicate log delivery. The spreadsheet and original Drive HTML
receive the same snapshot identity. The viewer receives only the redacted
projection, never private evidence, tokens, raw errors or permission snapshots.
Publication fetches a fresh proposal snapshot again so later human edits are preserved.
No server deployment is performed.

## Practical limits and verification

The runner uses ten workers by default, independent HTTP transports, serialized
durable journal writes and one process writer lock. Folder dependencies and
same-name collision guards constrain parallel work. Each HTTP request has the
existing 15-second socket timeout per attempt. A stale-socket GET/HEAD may
retry once; writes still allow only one transport send. This is not a strict wall-clock deadline for
a complete multi-request item or batch. Transient retries are bounded and follow
fresh reads. Cancellation stops new dispatch while retaining in-flight outcomes.

Drive clients outside this program are not locked. Fresh checks reduce races but
are not an atomic compare-and-swap guarantee; the program does not claim
exactly-once execution. The local hash-chained journal is tamper-evident, not
immutable against its owner. Preserve the original journal and verified backup
when recovering or moving the checkout.

Offline fake-Drive tests exercise selection, intact/mixed folders, collisions,
interrupted writes, resume, rollback and publication recovery. Read-only no-op
checks on completed sections verify current reads and selection without proving
that an untried section's live writes will succeed. Neither offline tests nor a
preview constitute a new live migration.

A bounded read-only check of already completed files is available without scanning
the whole section:

```sh
.venv/bin/python -m uqmc_recovery.destination '06 Trip Leaders/Skills and Assessment' --verify-completed 2
```

This samples 1–30 files and makes no whole-section completeness claim. The current
acceptance check sampled two files from each completed section (six files total),
plus one destination-anchor metadata read. All six matched current approved
name/parent state; checkpoint 2658 was unchanged. No new live migration was run.

Publication resolves canonical folder paths through the viewer's recorded original
and proposed names, rejecting ambiguous aliases. Completion of a registered
subsection contributes its verified sealed plan history to parent status. This
changes display aggregation only and never authorizes a migration. Publication
failures retain safe reason codes instead of only an exception class.
