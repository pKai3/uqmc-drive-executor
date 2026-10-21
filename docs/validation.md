# Validation evidence

Validated on macOS/Python 3.14.7, 2026-10-21.

`python3 -m unittest discover -s tests -v` passed **314 tests in 16.493 seconds**. All remote actions in these tests are synthetic fixtures or imported synthetic receipts. No actual Drive action was invoked.

| Area | Tests |
| --- | ---: |
| Journal integrity, durable evidence, locks and backups | 17 |
| Read request/receipt bridge | 15 |
| Fixture execution and rollback | 32 |
| Workbook importer | 38 |
| Section approval, exact names, method and origin | 63 |
| Section-to-agent metadata integration and cancellation | 8 |
| Partial metadata and agent plan validation | 23 |
| Agent rollback lineage and descendant chains | 16 |
| Fixture and agent action handoff | 49 |
| CLI boundaries and configured policy | 7 |
| Known duplicate-name preservation | 7 |
| MIME/name correction and exact restoration | 9 |
| Private/public projections and publication recovery | 30 |

The fixture engine covers 28 injected crash boundaries. The handoff suite injects failures before/after agent authority, preparation, attempted-before-ticket, result, uncertainty and reconciliation transitions. It checks unknown outcomes, completion before local receipt, consumed-ticket loss, known pre-dispatch refusal, dependency baselines, fresh receipts, known metadata omission, audience drift and named descendant drift. No fixture CAS behavior is claimed for the actual connector.

Collision tests preserve 32 pairs/64 distinct IDs, including restart between pair members. Name tests cover dotted stems, dates, missing/wrong historical extensions, PDF/PNG/HEIF MIME-evidenced correction, native Google display suffix rejection and exact inverse titles. Rollback tests reject unrelated manual baselines, duplicate inverses and active dependencies.

The CLI handoff demo independently produced one mock dispatch, RECONCILED_AT_TARGET, duplicate-ticket rejection and zero external calls. Private policy/evidence checkpoints were backed up and verified locally. The real journal has no dispatch approval, preparation or attempted action records. Its validation hold remains active and agent handoff is disabled.

The full section import-to-enable-to-cancelled-handoff integration now verifies both revocation and supersession, before preparation and before attempted dispatch. Strict snapshot tests cover exact field sets, nullable values, unique arrays, identity fingerprints, sealed dependency order and draft-only NOT_TRACKED semantics. The 26-column Operation Log transport is separate from both strict snapshots.

Read-only operational preparation captured the complete proposal tabs, metadata and parent listings for one approved section. The local manifest binds 423 separate field operations and preserves the existing conversational approval without changing worksheet decisions. The import remains blocked until independent review is bound to that exact capture. No source contents were downloaded and no source operation was attempted. Live capture also exposed fully and partially redacted exclusion rows: exact tombstones and quarantined references remain fingerprinted and cannot become executable records or reopen after later edits.

See [audit findings](audit-findings.md) and [handoff protocol](action-handoff.md) for remaining exact-manifest, provenance, fresh-read, backup and controller-release requirements. Tests do not certify a live pilot, hardware power-loss survival, permission restoration or exactly-once remote delivery.
