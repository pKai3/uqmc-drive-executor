# Safety and method audit

Audit date: 2026-10-21. This review uses local source inspection and synthetic fixtures. It made no Drive reads or mutations, prepared no real action, and published nothing. Counts below describe the scope reported by the coordinating parent; no real source identities or private evidence are included here.

## Current chosen method

The parent reports an explicit user choice of `existing_folder_consolidation` with `REUSE_EXISTING_FOLDER`: rename 241 files to their exact proposed unique titles, rename the reused root folder, reparent that folder, and move 180 separately enumerated incoming files into it. This is **242 separate RENAME operations and 181 separate MOVE operations**. Existing member files do not receive invented individual MOVE operations merely because their parent is reparented. The remaining original containers are preserved.

The parent also reports that the user accepted the described inherited-access exposure after a warning. That changes the relevant access-preservation condition; it does not authorise permission API edits, deletion, merging, destination substitution or public evidence sharing. A fresh destination remains an alternative and is not a prerequisite for the chosen REUSE method. Earlier fresh-folder requirements are superseded.

All file renames must precede moves, with separate status and evidence for every action. Correct final titles do not alone establish a safe intermediate order: source-parent name conflicts must be checked, and a rename depending on another file vacating a title must be ordered accordingly. A cycle blocks pending an explicit strategy; this system must not invent temporary names. Sources are selected by stable ID, never by title.

## Evidence and implementation status

| Item | What has been established | What has not been established |
| --- | --- | --- |
| Real inventory and method | The coordinating parent reports an independent actual-scope PASS. | This reviewer has not independently queried Drive or imported the real proposal. The final raw rename export and its exact per-ID bindings remain pending local validation. |
| Real policy | The parent reports `required_method_profile=existing_folder_consolidation`, `migration_paused=true`, `agent_handoff_enabled=false`, and no real authority records. CLI code checks the hold before real import, authorisation, preparation and attempt marking. | No real dispatch enablement or execution is cleared by this audit. |
| Import boundary | The wrapper binds the chosen method, source inventory, destination specification, projection and owner evidence through an external review context. Generic workbook import alone is insufficient to establish the intended method. | Hashes prove consistency, not the truth or independence of supplied human evidence. The current rename-and-move overlay is undergoing final review. |
| Metadata | The separate agent profile preserves missing/null fields honestly while requiring exact ID, name, MIME and complete parent IDs. It retains known grants and requires complete folder member scope. | Missing effective access, version or capability information is not silently proved complete. Accepted limitations must be recorded against the concrete batch. |
| Collision handling | Explicit stable-ID collision allowlists are proposal-hash-bound. Unknown third IDs block. The approved final rename scheme should remove target duplicates. | A historical same-name group does not authorise unseen IDs, overwriting or arbitrary duplicate names. The real rename ordering and current source-parent listings still require validation. |
| Execution seam | The bounded agent handoff records intent and ATTEMPTED before returning a single-use local ticket. It requires separate exact batch authority and controller review. Python invokes no connector. | A local ticket is not server idempotency. The read/write race remains; the controller must follow the protocol. No exactly-once guarantee is made. |
| Rollback | Fixture execution tests cover linked compensation in reverse dependency order, partial success, exclusions and manual drift. | The partial-metadata agent rollback path and extension-correction lineage are being added and require independent regression review before being called ready. Rollback cannot undo disclosure or guarantee permission propagation reversal. |

## Independent synthetic checks

- **23 agent-profile tests passed:** absent/null optional fields, retained known grants, complete parents and descendants, native display-suffix protection, ordinary and compound uploaded extensions, dotted filename stems, exact plan binding, durable exclusions and unresolved-request fences.
- **7 collision-policy tests passed:** 32 synthetic pairs containing 64 distinct IDs, interruption after the first member, restart without duplicate dispatch, unexpected third ID, changed plan hash, missing policy, excluded/self IDs and reverse moves to separate original parents.
- Earlier review independently checked the fixture engine and handoff crash/reconciliation suites, plus section/generic importer guards. Those results demonstrate local protocol properties; they do not validate the latest real rename manifest or a live connector run.

Final agent-handoff, rename-overlay and agent-rollback regression results will be recorded after their implementation review. No test above called Drive or fabricated a real execution success.

## Open findings and required readiness evidence

1. **High — concrete plan not yet locally validated.** Obtain the final raw export, independent stable-ID inventory/destination bindings, exact proposed names and relevant current metadata. Run the current method validator and reconcile the exact 242/181 action sets, all dependencies and exclusions. Freeze the resulting proposal hash; supersede earlier conflicting candidate artifacts instead of transferring their approval.
2. **High — latest integration work remains under review.** The CLI partial-metadata import selection, exact rename overlay, bounded agent handoff postflight checks and agent rollback lineage need passing integration tests. Report each as tested code or remaining work, never as a successful real operation.
3. **High — uncertain outcomes must stay fenced.** A timeout, lost ticket, lost receipt or crash after remote success cannot justify a replacement dispatch. An unchanged preimage is insufficient terminal proof. Reconcile against fresh stable-ID evidence and trusted completed-call provenance; if terminality is unavailable, leave the operation and dependent actions blocked. A completed call and matching target still do not prove exclusive attribution.
4. **Medium — known safety fields must not disappear in readback.** Preserve available identity, ownership and content markers. For RENAME, reject unexplained known access changes. For MOVE, distinguish accepted inherited-access effects from unrelated changes where the provider permits it, and retain explicit uncertainty when it does not. Bind available fields to actual raw receipts; never synthesize completeness.
5. **Medium — evidence provenance and manual handoff are trust boundaries.** The journal is durable, append-only through this program and hash-chained, but owner-editable local files are not cryptographic human authentication. An authorised controller must honestly bind trusted user/tool history and invoke only the emitted exact narrow arguments once. Local locks cannot prevent an external actor from copying a request or modifying Drive.

Before a real pilot, complete the actual validation, exact batch approval and scoped access/limitation binding, a separately reviewed release of the current hold, a private evidence backup, and the bounded execution/reconciliation/rollback rehearsal. Expired PREPARED reservations and genuinely unknown requests have no automatic retry or release. Workbook/HTML delivery remains separate from action success; private evidence must never enter the public viewer.
