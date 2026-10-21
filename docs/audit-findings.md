# Safety and method audit

Audit date: 2026-10-21. This review inspected local code and ran synthetic fixtures. It made no Drive reads or mutations, prepared no real action, and published nothing. No private source identities or evidence are included here.

## Current chosen method and real-state boundary

The coordinating parent reports the user's explicit choice of `existing_folder_consolidation` with `REUSE_EXISTING_FOLDER`: rename 241 files to their exact proposed unique titles, rename the reused root, reparent that root, and move 180 incoming files into it. This is **242 separate RENAME operations and 181 separate MOVE operations**. All file renames precede moves. Existing members have an inherited root MOVE effect, not invented individual MOVE operations; the remaining original containers are preserved.

The parent reports that the user accepted the described inherited-access exposure after a warning. This does not authorise permission API edits, deletion, merging, destination substitution or public evidence sharing. A fresh destination remains an alternative; the earlier mandatory-fresh-folder assumption is superseded.

The parent reports an independent actual-scope PASS. This reviewer has not independently queried Drive or imported the real proposal. The exact final raw rename export, per-ID title bindings and full concrete plan remain to be validated locally. The parent reports that real policy still has `required_method_profile=existing_folder_consolidation`, `migration_paused=true`, `agent_handoff_enabled=false`, and policy-only journal state with no real action or authority records. **This audit does not clear a pilot.**

## Reviewed changes

| Area | Reviewed safeguards |
| --- | --- |
| Intended method | Independent inventory and destination-specification evidence bind the reused physical folder, original/target parents, existing/incoming file partition and exact operation scope. Existing and incoming file identities cannot be substituted with folders. Sources are selected by stable ID. |
| Exact names | Every target is compared with the actual workbook proposed-name cell, not merely its row hash. Native Google display-suffix removal needs an exact MIME-specific conversion record. Uploaded extensions are preserved, with separately evidenced, hash-bound corrections for a verified MIME mismatch; changing a title does not convert file content. |
| Ordering and collisions | The root MOVE depends on every section rename. Incoming MOVEs depend on their rename and the root MOVE. Source-parent conflicts are checked; a conflicting intermediate rename blocks pending an explicit strategy. Exact collision ID allowlists cannot admit an unseen third ID or overwriting. |
| Authority | Section import origin, expiry, revocation and supersession are rechecked. CLI authorization, preparation and attempt marking enforce the configured method, including rejection of generic-plan revision bypasses. Separately approved inverses use verified historical section lineage, so cancelling forwards does not prohibit evidence collection or authorised recovery. |
| Metadata and access | Missing/null provider fields remain unknown. Raw known fields cannot be dropped or invented. Approved source/parent audience baselines are checked before preparation and marking; only verified dependency evidence can advance a baseline. Postflight preserves known immutable and member fields. Grant changes after a scoped MOVE are retained as observed; the connector cannot prove which changes were inherited. RENAME retains known grants, and unexpected parent access changes block. |
| Handoff and uncertainty | Durable ATTEMPTED precedes the once-issued local ticket. Receipt ingestion alone cannot establish success. Unknown requests stay fenced. Positive completion followed by an original-preimage read is a conflict, not proof of nonapplication. Only trusted evidence of pre-external-call non-dispatch plus an exact fresh preimage can establish that an attempted request was not sent; even then, its ticket is not reissued. |
| Rollback | Candidate construction is local and grants no authority. Inverse names/parents and preimages are linked to frozen verified originals, with reverse dependencies, active-dependent checks and duplicate-reversal protection. Hand-crafted manual baselines and arbitrary historical-title exceptions are rejected. Preparation/marking recheck lineage. Observed inherited grants are retained in the inverse baseline without pretending they were predicted. |

These guards close the implementation findings raised in this review. Their hashes establish evidence consistency, not the truth of supplied human statements or cryptographic identity.

## Test evidence

Independently rerun during the review:

- **63 section-importer tests** and **3 section-to-agent integration tests**, including wrong-but-consistent title bindings, native display conversions, full rename ordering, exclusions and section-authority changes.
- **49 handoff tests**, including crash transitions, single-use tickets, completed-success/preimage disagreement, raw known-field omissions, audience drift, approved dependency audience refresh, named descendant/sibling drift and explicit parent-membership semantics.
- **15 agent-rollback tests** and **9 naming-policy tests**, including exact historical extension restoration, manual inverse-preimage rejection, duplicate rollback, partial success and root/child reverse ordering.
- **23 agent-profile tests** and **7 collision-policy tests**, including honest optional metadata and 32 synthetic collision pairs containing 64 distinct IDs.

The parent reports **280 passing tests** in its complete-suite run. These are synthetic protocol and code checks. They are not a live connector rehearsal, actual workbook validation or evidence that a club action occurred. Section expiry/revocation/supersession tests exercise the validator and the handoff call sites were inspected; a complete section-import-to-cancelled-handoff test is not included.

## Remaining real-pilot requirements and limits

1. Validate the final actual export and evidence with the current importer. Reconcile the exact 242/181 action sets, all 241 file IDs, root/destination identities, exact real titles, native-display/MIME-correction evidence, exclusions and current source-parent collisions. Freeze the resulting proposal revision/hash and supersede conflicting older candidates.
2. Bind the existing applicable user authority to that exact concrete batch, including the scoped audience warning, accepted unavailable-provider fields and residual race limitations. Any scope outside that authority needs its own approval. Complete the separate controller readiness review before releasing the current hold.
3. Capture fresh stable-ID source/parent reads, bounded complete membership and current grants/conflicts immediately before dispatch. Take a private durable backup and confirm the recovery procedure. Python supplies no standalone connector transport; an authorised agent must invoke only the emitted narrow arguments once, then ingest the actual receipt and reconcile.
4. Perform a bounded first run only after the coordinating task accepts the review and releases the hold for the already authorised concrete scope. Any rollback needs its applicable separate scoped authority. No such pilot has occurred. Expired PREPARED reservations, a lost consumed ticket or a genuinely unknown request cannot be automatically retried or released. A known denial is not permission to bypass the denial or create new authority.
5. Keep workbook/HTML delivery separate from action outcome, and keep private evidence outside the public viewer. Publication failure does not roll back a successful Drive action.

The connector lacks atomic conditional updates. Fresh reads and local locks do not remove the read/write race, prevent another actor editing Drive, or stop a person copying a tool request. Local files are owner-editable despite hash chaining. No exactly-once guarantee is made. Rollback preserves verified names/parent intent and refuses unexplained drift; it cannot undo disclosure or guarantee restoration of permission propagation.
