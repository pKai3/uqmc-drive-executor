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

A type suffix in a current-name display column is not always part of the actual
Drive title. Uploaded-file display suffixes are accepted only when the same-ID
Move Manifest title and both recorded source paths exactly corroborate the live
name, and the suffix matches the live MIME type. Targets remain exactly as
approved; missing evidence or a different live title remains a conflict.

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

Uploaded extensions are compared without letter-case differences, so an approved
`.JPG` to `.jpg` change is accepted without changing the file format. Genuine
extension changes still require the existing MIME/provenance validation. Exact
observed and target titles remain in recovery history.

Before creating destination folders, the command validates proposed rename titles
and extensions. Import failures report reason counts and proposal references;
the private console log retains every blocked row. Checks requiring allocated
folder IDs still run during the final import.

Independent folder creation and verification use the configured bounded worker
count (default 10), with separate HTTP transports. Children wait for their parent
to be verified. The process writer lock stays held and durable journal writes are
serialized, while network requests overlap. Failure stops new scheduling and
in-flight outcomes are recorded. Restart reconciles previously allocated folder
IDs before any retry; it does not create replacement identities. If preparation
fails before a saved executable batch exists, rerun ordinary `--apply` after
resolving the reported blocker. Use `--resume` only for an existing saved batch.

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

A live child absent from the proposal inventory and explicit routes stops with
`WHOLE_SUBTREE_CHILD_MISSING_PROPOSAL_COVERAGE`. The exception is an exact folder
ID allocated and verified in this journal during earlier destination preparation,
whose current name, parent, stable creation key and currently approved path still
match. Its unplanned contents remain blockers. This proves the destination's
identity; it does not add source files or reuse an old execution approval.

A smaller subtree may reuse an ancestor batch's verified destination identity
only when its recorded logical path falls inside that subtree. The new batch
still reads its own fresh proposal and binds its own approval. Incoming items
wait for moving destination ancestors, including ancestors above generated child
folders. Ordinary `--apply` recovers a failed preparation; no special mappings or
recreation are needed. Preview without `--apply` is read-only.

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

For a pending explicit rename, a different starting title is a warning: the
approved target name wins for that stable file ID. The frozen batch and durable
mutation intent retain the actual observed name for rollback; a separate audit
event preserves the historical/proposal name and warning. Move-only records,
completed effects and unresolved requests do not gain this exception. Parent,
identity, content, capability and collision checks remain active. A change after
the batch is frozen still blocks the write and requires a fresh reconciliation.

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
Server sync is disabled unless the optional project configuration below enables it.

## Optional mounted-share server sync

The default is off. To use the existing mounted share, set only these optional
keys in the ignored project configuration; the destination must already exist:

```json
"server_sync": {
  "enabled": true,
  "destination_directory": "/absolute/path/to/existing/mounted/share"
}
```

The publication command builds and validates `viewer.html` once in its private
publication directory. It attempts spreadsheet delivery independently, then syncs
that exact local artifact to the share's fixed `index.html`, verifies its SHA-256,
and only then uploads the same bytes to the original Drive HTML file. It never
uses Drive Desktop as the server source. No new credentials, SSH integration or
mount creation are involved. The existing standalone shell script remains usable.

The transfer uses `rsync -c --progress -- SOURCE DEST/index.html` as an argument
array, without a shell. Default rsync temporary-file/rename behavior is retained;
there is no `--inplace`. The local rsync subprocess has a 60-second timeout,
separate from Google's 15-second socket timeout. The wrapper kills and waits for
rsync on timeout, then checks the destination hash. Filesystem access to an
unresponsive mounted share can still block at the OS level; the subprocess limit
is not a deadline for every filesystem operation or the whole publication.

A missing share, invalid/empty/unsealed HTML, symlink target or destination,
changed server preimage or newer verified publication blocks replacement. The
program creates no destination directory. One project lock serializes transfers.
A small private target-wide index records intent before transfer and verification
after readback. It prevents an older unsent publication from overwriting a newer
delivery, including a crash after copying but before its success receipt. Receipt state stays in `server-sync.json`
alongside the local publication, with a separate stage in `publication.json`;
private receipts are never copied to the share. A normal remount with the same
canonical path and verified target hash can reconcile without another copy.
There remains a filesystem race with external writers after the last precheck;
the program does not provide a remote lock or exactly-once guarantee.

Server failure leaves new Drive HTML delivery `WAITING_FOR_SERVER`; spreadsheet
success and migration results remain recorded. A previously verified Drive copy
is not downgraded or reuploaded merely because server sync was enabled later.
The final console reports spreadsheet, server and Drive states separately.
The aggregate publication is verified only when every enabled stage is verified.

Check or retry **only the server**, without Google calls or a migration:

```sh
.venv/bin/python -m uqmc_recovery.server_sync --publication private/destination-publications/DELIVERY_DIRECTORY --check
.venv/bin/python -m uqmc_recovery.server_sync --publication private/destination-publications/DELIVERY_DIRECTORY --apply
```

`--check` validates local evidence and reads the destination hash without upload
or receipt changes. `--apply` is explicit server-write authority. Both commands
use the existing private configuration by default; `--config PATH` selects it
explicitly. After server recovery, the earlier destination `--publish-only` command
reconciles the server checksum and finishes Drive delivery without replaying a
migration or a verified transfer.

If interruption left an `ATTEMPTED`/`UNCERTAIN` transfer and the target still matches
its recorded preimage, first confirm the old rsync process has stopped. Only then
use the server-only `--apply --confirm-transfer-stopped` option. It still rejects
changed target contents. A target already matching the source is verified without
retransfer. Do not delete receipts to bypass a conflict or stale-publication guard.

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

## Trailing whitespace in destination folders

Folder titles are exact values. Before adopting or creating a destination, the
runner rejects a sibling whose name differs only by trailing whitespace, including
an exact-name folder beside a spaced variant. It does not silently merge, rename,
or resolve those identities. A collision must be resolved explicitly by stable ID.

The internal `folder_cleanup.cleanup_folder` helper performs separately authorized
trailing-whitespace corrections only. Its sealed authority lists exact folder IDs,
before names, target names and unchanged parent IDs. Run it under the destination
command lock and journal lock. It checks live state and sibling collisions, journals
intent before a name-only update, and verifies readback. Restart reconciles a prior
attempt; an unresolved unchanged request is never automatically sent again.
A change after verified success is a conflict. Existing frozen plans stay intact;
current destination observations may be appended by stable ID after verification.
Match any sheet corrections to exact destination references, preserving formulas,
approvals, source filenames and literal slashes. Do not trim arbitrary path cells.


## Retiring a user-confirmed deleted item

When an item is explicitly confirmed deleted, keep its existing proposal row and
ID for audit. Put `[RETIRED: USER_CONFIRMED_DELETED]` on the first line of its
**Evidence / rationale** cell, retain the old rationale below it, and keep the
Decision **No** and existing review formula. This is not a deletion request.
The importer records a typed `RETIRED` disposition and excludes that ID from
execution, including whole-subtree fallback. Later removal of the marker does
not reopen a durable retirement.

Publication keeps the allowlisted audit item labeled **Retired — user-confirmed
deleted; not migrated**, while excluding it from active completion counts. It
does not invent a verified name, location, rename or move. Ordinary **No**
decisions and recoverable missing/inaccessible exclusions remain outstanding;
private exclusions remain private. Existing journal history is preserved.

## Explicit revisions of completed destinations

Ordinary `--apply` still refuses to silently replace completed targets. For a
separately approved layout or filename revision, use a private manifest bound to
the **fresh canonical sheet after the proposal edits**:

```sh
.venv/bin/python -m uqmc_recovery.destination '02 Meetings' --revision-manifest private/reviews/meetings-year-first-20261022/revision-manifest.json
.venv/bin/python -m uqmc_recovery.destination '02 Meetings' --revision-manifest private/reviews/meetings-year-first-20261022/revision-manifest.json --apply
```

The first command previews. The second is explicit authority to execute only
that exact revision. Neither a manifest nor its hash alone confers approval.
The manifest schema is `uqmc.destination.revision.v1`:

```json
{
  "schema_version": "uqmc.destination.revision.v1",
  "revision_id": "year-first-reviewed-v1",
  "section_path": "Section",
  "workbook_id": "canonical-workbook-id",
  "input_sha256": "fresh-catalog-input-sha256",
  "items": [{
    "source_id": "stable-existing-id",
    "before_path": "Section/Category/2020",
    "target_path": "Section/2020/Category",
    "target_name": "Category"
  }],
  "new_folders": [{"path": "Section/2020", "name": "2020"}]
}
```

`input_sha256` is the `input_sha256` returned by `importer.catalog` for the saved
fresh workbook capture, with `include_subtree_coverage=True` and
`subtree_section_path` equal to the section. Unchanged sheet binding is required;
if the proposal changes, review and regenerate the manifest rather than bypassing
the mismatch. IDs and original paths must have verified prior journal/registry
provenance. New folder entries must be required ancestors of the exact targets.
A same-name unrecorded destination is an ambiguity, not an ID to adopt.

The command creates a new plan, run and operation IDs, preserves old completion
references, and records freshly observed before names/parents for rollback.
Previously journal-created folders are moved/renamed by their existing IDs.
Their descendants are checked against the fresh sheet by stable parent ID and
exact name, then carried intact without new file operations or reopened
completion records. Unrelated completed work stays complete. Unknown children,
changed child targets, actual source drift and unresolved old requests block.
An interrupted folder creation is reconciled against its previously reserved ID.

After a saved revision is interrupted, resume its exact frozen plan:

```sh
.venv/bin/python -m uqmc_recovery.destination '02 Meetings' --resume --apply
```

Do not start another revision while a request remains uncertain. Resume reads
current state before retrying and never replays an already satisfied effect.
For an explicit rollback of this revision alone:

```sh
.venv/bin/python -m uqmc_recovery.destination '02 Meetings' --rollback REVISION_RUN_ID --apply
```

Rollback restores the revision's recorded starting names/parents only if the
current state still matches its applied state. It does not undo earlier batches,
delete newly created parents, or automatically run on failure. Preview and
execution use existing credentials, timeout, bounded worker pool, journaling and
publication recovery. Offline fixture tests prove the workflow; a new revision
is not live-tested until the operator runs it.


## Automatic empty-parent cleanup

Only after an explicitly requested rearchitecture (`--revision-manifest`) has
successfully applied/resumed does the destination command check old direct
parents emptied by that exact sealed revision. Normal migrations skip cleanup.
Every candidate must be proven beneath the configured New File Structure
`root_id` through fresh parent IDs; names and logical path prefixes are not
containment evidence. The root itself is always protected. Missing, ambiguous,
unavailable or outside-tree ancestry leaves the folder untouched.
It processes nested candidates from
children upward; it does not crawl for unrelated empty folders. Roots, exclusions,
fresh planned destinations, active source assignments and other registered
sections are protected. An explicitly retired/deleted proposal remains audit
history and does not reserve its obsolete destination forever. Cleanup is skipped
if execution stopped/failed, rollback ran, or the workbook revision changed.

Each candidate needs its captured ID/name/parents/MIME to match a fresh read,
`canTrash=true`, and a complete empty immediate-child listing. Cleanup records
intent before one `files.update` containing only `trashed: true`, then verifies
readback. It never permanently deletes, changes sharing, or blindly retries an
unresolved write. Remote edits can still race between listing and update because
Drive provides no atomic "trash only if empty" operation; avoid concurrent manual
changes during the run. Cleanup results are separate from migration results and
are backed up before publication. Original events and rollback preimages remain.

For a specific completed revision, preview/reconcile only its candidates with:

```sh
.venv/bin/python -m uqmc_recovery.revision_cleanup --run-id REVISION_RUN_ID
```

Add `--apply` only to perform that explicitly selected cleanup. This command is
also the recovery path for a cleanup interruption; it reads the exact IDs first.
It does not repeat an unresolved unchanged request. A failed capability or unknown
identity leaves the folder untouched.

Rollback does not automatically restore parents from Trash. If it reports
`ROLLBACK_REQUIRES_RESTORE_TRASHED_PARENTS`, restore those exact IDs in Drive first,
then repeat the rollback command. A fresh exact metadata read records the restore
without erasing retirement history. Restored parents are not trashed again by the
same cleanup run. A changed name/parent or unavailable identity stays blocked.
Google normally permanently deletes items after 30 days in Trash, so recoverable
trashing is not an indefinite backup. Retain the private journal/evidence backups.
