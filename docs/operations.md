# Operations, backup and remaining pilot requirements

## Current safety boundary

Only implementation, local fixture runs and read-only connector checks are configured. The initial real journal contains policy/evidence records and no source action events. No live execution approval is stored. Existing shared workbook, plan document and viewer integration belong to the coordinating task.

The discovered metadata tool accepts a `fields` selector, but the observed normalized response omitted requested `version`, `trashed`, `owners` and full `capabilities`. It provided title, MIME type, parent IDs, modified time and a permission summary. Permission entries omitted permission IDs/inheritance provenance; `permissionIds` and `inheritedPermissionsDisabled` were also absent. A permission list does not establish complete effective access. No separate list-permissions tool was exposed in the available connector inventory.

The metadata update tool offers `fileId`, `name`, `addParents` and `removeParents`; it does not expose a conditional version/ETag precondition or a terminal request identifier. Assistant tools are session capabilities, not reusable credentials for this Mac program. No tokens, OAuth clients, service accounts, scopes or new connector permissions were created.

## Backups and restore

Before a future approved batch, create a verified local checkpoint using `backup`. After any settled batch and after interrupted-run reconciliation, create another checkpoint. Keep the source journal and evidence together. Snapshot creation holds the writer lock, verifies referenced evidence, fsyncs its files, and marks completion only after writing the manifest. Incomplete snapshots cannot be opened as usable stores.

The configured local backup directory is ignored and private. No automatic scheduler or off-device backup is enabled. This protects against some software errors, not disk loss. Before a pilot, the owner must identify an existing private encrypted backup destination (for example an already-authorised encrypted Time Machine volume), confirm its access and restore procedure, and retain checkpoints there. Do not create remote sharing to satisfy this requirement.

`Journal.recover_to(new_directory)` preserves the original bytes, saves a forensic copy, and salvages only complete verified records before an incomplete terminal line. It never truncates the original. Interior corruption or a bad hash fails closed. Never copy a backup over the active store. Restored stores are marked by a manifest and the engine refuses dispatch from them. A later recovery implementation must reconcile every potentially affected source and uncertain request against live state, obtain fresh batch approval, and establish a new active store without erasing old evidence. A backup may predate successful remote actions.

## Pilot requirements: all still pending

1. A separate explicit human batch approval for a small, fixed selection, bound to the imported proposal revision/hash and exact move/rename fields. Rollback needs its own explicit scope and approval. The configuration request is not either approval.
2. A supported connector transport under existing authorised access. Do not invent a standalone connection or extract credentials. Current agent-assisted CLI covers metadata reads only. Implement and review a future prepare/dispatch/receipt/reconcile mutation handoff before any source write.
3. Complete live source, original-parent and destination metadata: stable IDs, exact titles, MIME, parent arrays, version/time, trash/owner/drive state, actual capabilities and full permission baselines. Native Google titles must not acquire display suffixes; uploaded file extensions must survive.
4. Complete effective-access and descendant evidence, including inheritance, limited-access restrictions, destination audience and all paginated membership. Any excluded descendant blocks an ancestor action; never open a permanently withdrawn item to resolve a gap. Any access change needs explicit approval of that exact reviewed difference.
5. An enforceable concurrency/precondition guard or a separately reviewed operational alternative. Two reads alone do not eliminate the race between check and write. Unknown request outcomes need terminal/quiescence evidence before retry or compensation; a matching snapshot alone is insufficient.
6. Verified backup/restore readiness and adequate private disk space. Confirm durable local storage is the intended location and establish an approved off-device recovery copy before relying on it against hardware failure.
7. Agreed workbook/viewer projection installation with readback and delivery acknowledgements. Check snapshot ID/watermark parity and make stale/unknown status visible. A sync retry must never invoke an action. Evidence remains outside the viewer.

## Native API references

[Google Drive sharing guide](https://developers.google.com/workspace/drive/api/guides/manage-sharing) documents inherited access and its reevaluation on moves. [Drive files.update reference](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update) documents field-specific metadata updates. The connector's observed surface is narrower than the underlying API. This project does not claim broader connector support.
