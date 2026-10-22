# Publish completed execution status

This updates tracking only. It does not rename, move, create or delete Drive
source items, change permissions, refresh migration approvals, or start a batch.
The original hash-chained journal remains unchanged; delivery preimages, intent
and readback receipts are saved under ignored private storage.

1. Freeze one publication bundle from a known checkpoint:

   ```sh
   .venv/bin/python -m uqmc_recovery.publication_projection \
     --journal private/state --checkpoint CHECKPOINT \
     --identity-basis private/viewer-identity-basis.json \
     --sections private/publication/sections.json \
     --output-directory private/publication
   ```

   The private sections file binds each section's exact frozen plan, expected
   source IDs and destination. Do not infer completion from an editable sheet.
   Newly created destinations appear in the separate section overlay; they do
   not become original source identities.

2. Preview the spreadsheet update, then repeat with `--apply`:

   ```sh
   .venv/bin/python -m uqmc_recovery.sheet_publication \
     --bundle private/publication/SNAPSHOT-publication-bundle.json \
     --workbook-id WORKBOOK_ID --token secrets/token-drive.json \
     --output private/publications/CHECKPOINT
   ```

   The publisher uses the existing explicit full-Drive grant for the Sheets
   API; it never creates credentials or requests a new scope. It joins by stable
   source ID, checks headers and existing data, preserves original proposal and
   decision columns, appends missing Operation Log event IDs, and adds dedicated
   execution columns plus Migration Summary. Names are literal cell values,
   including names beginning with `=`. New columns containing unlabelled user
   data block publication. A fresh read must match the prewrite capture before
   one atomic Sheets batch is sent. Sheets offers no compare-and-swap, so a small
   concurrent edit window remains; original proposal values are checked again
   afterward, and readback conflicts are reported rather than repaired blindly.

   Every affected range is read back, including after an uncertain write
   response. Do not blindly replay a failed delivery: inspect `result.json` or
   read the remote event IDs/snapshot before rerunning. A retry deduplicates
   historical log events. Publication failure never changes action success.

3. Feed only the bundle's redacted `html` and explicit `sections` projection to
   the existing viewer updater. Both surfaces retain the same snapshot UUID.
   Never serve the bundle itself, private workbook/log exports, raw journal,
   permission evidence or credentials. Preserve the viewer's original source
   identity allowlist and exclusions. Readback timestamps describe a static
   checkpoint, not continuous live Drive verification.

The journal evidence retains historical failures and uncertain requests. Latest
verified item effects may supersede them; they must not be displayed as still
pending. No system record means NOT_TRACKED, not proof that a file has stayed in
its original location. A child relocated with a folder has an inherited location
effect, not a direct child move.

Recoverable execution exclusions are distinct from private withdrawals. An
already-public, explicitly allowlisted item marked `recoverable: true`,
`effect: NOT_APPLIED`, and `reason_code: MISSING_OR_INACCESSIBLE` remains visible
as excluded/not migrated. It remains in the expected section count and cannot
make the section complete. Any separate private/default exclusion still removes
it from public output. The shared automatic CLI and publication-only paths use
this same projection; they never retry file migration to repair publication.

A public-membership conflict now reports
`SECTION_IDENTITIES_NOT_PUBLIC_ALLOWLISTED` with a console explanation instead
of only `ValueError`. Private evidence and raw API error text stay private.

An unchanged destination folder may have no mutation event. Publication can use
its explicit `DESTINATION_VERIFIED` observation only when it matches the exact
sealed target ID, name, parent IDs, MIME type and registry path. This fallback
cannot replace a later uncertain or conflicting action result. Verified child
files can therefore be displayed beneath a reused no-op folder without inventing
a folder move or weakening destination verification.
