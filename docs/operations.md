# Operations, backup and remaining pilot requirements

## Current safety boundary

Only implementation, local fixture runs and read-only connector checks are configured. The initial real journal contains policy/evidence records and no source action events. No live execution approval is stored. Existing shared workbook, plan document and viewer integration belong to the coordinating task.

The discovered metadata tool accepts a `fields` selector, but the observed normalized response omitted requested `version`, `trashed`, `owners` and full `capabilities`. It provided title, MIME type, parent IDs, modified time and a permission summary. Permission entries omitted permission IDs/inheritance provenance; `permissionIds` and `inheritedPermissionsDisabled` were also absent. A permission list does not establish complete effective access. No separate list-permissions tool was exposed in the available connector inventory.

The metadata update tool offers `fileId`, `name`, `addParents` and `removeParents`; it does not expose a conditional version/ETag precondition or a terminal request identifier. Assistant tools are session capabilities, not reusable credentials for this Mac program. No tokens, OAuth clients, service accounts, scopes or new connector permissions were created.

## Backups and restore

Before a future approved batch, create a verified local checkpoint using `backup`. After any settled batch and after interrupted-run reconciliation, create another checkpoint. Keep the source journal and evidence together. Snapshot creation holds the writer lock, verifies referenced evidence, fsyncs its files, and marks completion only after writing the manifest. Incomplete snapshots cannot be opened as usable stores.

The configured local backup directory is ignored and private. No automatic scheduler or off-device backup is enabled. This protects against some software errors, not disk loss. Before a pilot, the owner must identify an existing private encrypted backup destination (for example an already-authorised encrypted Time Machine volume), confirm its access and restore procedure, and retain checkpoints there. Do not create remote sharing to satisfy this requirement.

`Journal.recover_to(new_directory)` preserves the original bytes, saves a forensic copy, and salvages only complete verified records before an incomplete terminal line. It never truncates the original. Interior corruption or a bad hash fails closed. Never copy a backup over the active store. Restored stores are marked by a manifest and the engine refuses dispatch from them. A later recovery implementation must reconcile every potentially affected source and uncertain request against live state, obtain fresh batch approval, and establish a new active store without erasing old evidence. A backup may predate successful remote actions.

## Current pilot gates

A section-level movement approval can authorize the exact derived operations. It does not require invented `Yes` cells or a new human confirmation of a hash. The journal binding must preserve that approval's original reference, exact reviewed source set, destination mode, operation fields and any explicit access exposure acceptance. A changed section or later added source IDs do not inherit that approval.

Before actual dispatch, the coordinator must:

1. Supply the complete current workbook export, independently reviewed membership and destination mapping, explicit REUSE or NEW_FOLDER_REQUIRED mode and exact operation dependencies. Retain the original containers. The selected section's current proposal must be independently validated; passing synthetic tests is not that validation.
2. Import and inspect the frozen plan with the section approval overlay. Bind allowed duplicate-name peer IDs to exact names and target parents. Preserve every distinct identity. Record access differences and available grants; scoped acceptance of exposure does not authorize permission API edits.
3. Supply a reviewed dispatch-enable record for one controller and the frozen plan, then explicitly release the local validation hold. Local defaults are `migration_paused=true` in the configured project and `agent_handoff_enabled=false`. The configuration request alone does not enable dispatch.
4. Obtain fresh source, original-parent, target-parent, descendant/membership and collision evidence immediately before each ticket. Stable IDs, exact names, MIME and parent relationships are mandatory. Missing version, trash, owner, capability or inheritance fields remain unknown and are recorded as limitations. Known conflicting metadata or missing critical identity evidence blocks. Unknown move capability may result in a terminal refusal; inverse capability is not guaranteed in advance.
5. Use one assistant controller, issue the exact returned narrow tool arguments once and save the raw result before independent readback. The connector has no atomic CAS, so fresh reads reduce but cannot remove the check/write race. Any suspected concurrent change blocks. A timeout, lost ticket or crash remains uncertain and cannot be retried merely because the old location is observed.
6. Create and verify a local checkpoint. Retain a private recovery copy using an existing authorised backup destination if protection from disk loss is needed; no new remote sharing is configured.
7. Read back the action and publish only derived statuses through the agreed workbook/HTML interface. Retry publication independently, never by rerunning an action. Compare snapshot IDs and watermarks. Evidence and permission baselines remain private.

The command workflow and exact trust boundaries are in [action handoff](action-handoff.md). Code cannot authenticate a human by itself: provenance evidence and supplied tool receipts are assertions from the trusted controller, not signed provider attestations. No real pilot has been executed by this project.

## Native API references

[Google Drive sharing guide](https://developers.google.com/workspace/drive/api/guides/manage-sharing) documents inherited access and its reevaluation on moves. [Drive files.update reference](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update) documents field-specific metadata updates. The connector's observed surface is narrower than the underlying API. This project does not claim broader connector support.
