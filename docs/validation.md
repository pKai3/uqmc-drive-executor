# Validation evidence

Validated on macOS/Python 3.14.7, 2026-10-21.

`python3 -m unittest discover -s tests -v` passed **280 tests in 14.492 seconds**. All remote actions in these tests are synthetic fixtures or imported synthetic receipts. No actual Drive action was invoked.

| Area | Tests |
| --- | ---: |
| Journal integrity, durable evidence, locks and backups | 17 |
| Read request/receipt bridge | 15 |
| Fixture execution and rollback | 32 |
| Workbook importer | 21 |
| Section approval, exact names, method and origin | 63 |
| Section-to-agent metadata integration | 3 |
| Partial metadata and agent plan validation | 23 |
| Agent rollback lineage and descendant chains | 16 |
| Fixture and agent action handoff | 49 |
| CLI boundaries and configured policy | 6 |
| Known duplicate-name preservation | 7 |
| MIME/name correction and exact restoration | 9 |
| Private/public projections and publication recovery | 19 |

The fixture engine covers 28 injected crash boundaries. The handoff suite injects failures before/after agent authority, preparation, attempted-before-ticket, result, uncertainty and reconciliation transitions. It checks unknown outcomes, completion before local receipt, consumed-ticket loss, known pre-dispatch refusal, dependency baselines, fresh receipts, known metadata omission, audience drift and named descendant drift. No fixture CAS behavior is claimed for the actual connector.

Collision tests preserve 32 pairs/64 distinct IDs, including restart between pair members. Name tests cover dotted stems, dates, missing/wrong historical extensions, PDF/PNG/HEIF MIME-evidenced correction, native Google display suffix rejection and exact inverse titles. Rollback tests reject unrelated manual baselines, duplicate inverses and active dependencies.

The CLI handoff demo independently produced one mock dispatch, RECONCILED_AT_TARGET, duplicate-ticket rejection and zero external calls. A private six-event policy/evidence checkpoint was backed up and verified locally; the real journal has no approval, preparation or attempted action records. Its validation hold remains active and agent handoff is disabled.

Independent code review found no remaining blocker in the reviewed implementation delta. Actual raw evidence for the 423-operation section has not been imported locally. Section cancellation/expiry/supersession is tested at the authority validator and its handoff call sites were inspected; a full section-import-to-cancelled-handoff integration is not included. Read-only live work in this task inspected workbook metadata/schema only.

See [audit findings](audit-findings.md) and [handoff protocol](action-handoff.md) for remaining exact-manifest, provenance, fresh-read, backup and controller-release requirements. Tests do not certify a live pilot, hardware power-loss survival, permission restoration or exactly-once remote delivery.
