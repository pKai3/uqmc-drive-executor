# Guarded agent action handoff

This is a runnable **local handoff protocol**, not a standalone Drive connection. Python never invokes a connector, reads credentials or sends a remote request. Defaults disable ticket preparation. The configured real store is paused with agent handoff off pending review of the exact manifest and evidence.

`TEST_ONLY` uses a separate store and a fake tool. `AGENT_ASSISTED` produces an exact ticket for an assistant to invoke through the existing authorised connector. It requires independently reviewed enablement and a frozen approval first. A local ticket is not server idempotency, an atomic precondition or proof of human consent. The code and synthetic tests alone do not clear a real pilot.

## Exact integration sequence

1. Collect the explicitly selected proposal rows and current metadata through already-authorised reads. Preserve unknown provider fields as unknown. Import a frozen manifest with stable operation and file IDs, exact before/target fields, dependencies and revision/hash. Keep original and destination containers, child membership and available grants in private evidence. An intact folder move is one root action; its descendants do not each get an invented move.
2. Bind the human approval to that exact batch, operation set and target fields. Record any explicitly accepted limitations separately. An approval to move does not approve a rename, a permission API call, creation/deletion or a second source. A later access-preservation waiver can alter the access policy without granting permission edits or authorising undisclosed new audiences.
3. `prepare_action` verifies the frozen plan, approval, exact current preimage, preflight and dependency state. It seals evidence and reserves the affected scope. The envelope binds the plan hash, operation, approval, exact narrow tool arguments and evidence hashes. It is not yet a dispatch ticket.
4. Immediately before the external tool call, obtain a fresh stable-ID read and recheck the original/destination parents, membership, exclusions, approval and conflicts. Call `mark_attempt` with the envelope hash and this evidence. It durably records `ATTEMPTED` before returning one ticket. The marking step is consumed even if the process crashes before printing the ticket.
5. The assistant invokes the exact ticket once. Agent tickets allow only `mcp__codex_apps__google_drive_update_file` and either `{fileId, name}` or `{fileId, addParents, removeParents}`. No evidence, permission, content, MIME or ownership arguments are added. Fixture tickets name a fake tool and must never be substituted into the real connector. Save the unmodified response, errors and completion provenance privately.
6. `ingest_result` requires the exact envelope hash and attempt key. Identical receipt retries are idempotent; conflicting reuse is rejected. A successful-looking response alone is not verified action success. Receipt ingestion after an approval expires must remain possible so later evidence is not lost.
7. `reconcile_action` requires independent readback, postflight evidence and request-terminal evidence. Compare exact source ID, name, MIME and parent relationships; retain membership and available access observations. A third state is a conflict. A matching target is observed at target with attribution uncertainty. An unchanged preimage plus a proven terminal request supports not applied; an unchanged preimage alone does not permit another dispatch.
8. Publish only derived status through the existing private workbook/redacted HTML contract. Delivery failures do not change the action outcome. Rollback is a new linked action under separate approval and fresh conflict checks.

## CLI protocol

All input files and outputs belong in ignored `private/`. Start with `python3 -m uqmc_recovery handoff-demo` for a complete runnable example. It bootstraps a new synthetic store, freezes its plan, stores a fixture concurrency proof, runs all four stages, verifies that duplicate ticket issuance is rejected, and saves the exact stage inputs. It calls no external tool.

The following individual process-boundary commands use synthetic data and a separate `private/handoff-fixture-state` journal. They require a matching content-addressed fixture concurrency proof already stored in that journal; the demo and `tests/test_action_bridge.py` show its construction. `prepare-action --fixture-mode` freezes the supplied fixture plan. Copying an input JSON file without its matching evidence store is insufficient and must fail:

```sh
python3 -m uqmc_recovery prepare-action --fixture-mode --input private/fixture-prepare.json
python3 -m uqmc_recovery mark-attempt --fixture-mode --input private/fixture-mark.json
python3 -m uqmc_recovery ingest-result --fixture-mode --input private/fixture-result.json
python3 -m uqmc_recovery reconcile-action --fixture-mode --input private/fixture-reconcile.json
```

The prepare input contains `plan`, `operation_id`, `approval`, `before` and `preflight`. The mark input contains `envelope_hash`, `observed_before` and `preflight`. The result input contains `envelope_hash`, `attempt_key`, `raw` and optional `receipt_id`. The reconcile input contains `envelope_hash`, `observed`, `postflight` and `terminal_evidence`. The fixture tests provide complete input examples; a hash is a binding, not human identity or consent.

## Agent mode and evidence delivery

Save each raw connector result as a private JSON file and store it durably:

```sh
python3 -m uqmc_recovery evidence --input private/raw-read.json
```

This returns a content-addressed reference. Store normalized snapshots, user-instruction provenance, independent inventory/mapping review, collision lists, membership pagination and other referenced records the same way. Do not alter raw connector responses or insert approval text into tool arguments. `tests/test_agent_action_bridge.py` is the executable reference for evidence envelopes; all IDs there are synthetic.

Import complete workbook rows with a conversational section overlay and honest optional metadata:

```sh
python3 -m uqmc_recovery import-section --input private/workbook.json --projection private/section.json --owner-approval-ref APPROVAL_REF --review-context-ref REVIEW_REF --section-id SECTION --metadata private/metadata.json --resolutions private/resolutions.json --batch-id BATCH --metadata-profile AGENT_ASSISTED
```

The current pause blocks this command on real state. Import never creates an execution approval. For a reviewed and separately released batch, the following commands perform only local journal work:

```sh
python3 -m uqmc_recovery authorize-agent-dispatch --controller-id CONTROLLER --input private/authorize.json
python3 -m uqmc_recovery prepare-action --agent-mode --controller-id CONTROLLER --input private/prepare.json
python3 -m uqmc_recovery mark-attempt --agent-mode --controller-id CONTROLLER --input private/mark.json
# The assistant invokes the exact returned tool/args once, then saves its raw result.
python3 -m uqmc_recovery ingest-result --agent-mode --controller-id CONTROLLER --input private/result.json
python3 -m uqmc_recovery reconcile-action --agent-mode --controller-id CONTROLLER --input private/reconcile.json
```

`authorize.json` contains the frozen `plan`, `approval` (mode `AGENT_ASSISTED`), `authorization_evidence` and `enable_evidence`. The authorization binds the actual trusted user instruction, exact proposal and approval hashes, scoped exposure acceptance and documented connector limits. The separate enable record binds controller, reviewed manifest, reviewer/provenance and expiry. The existing section approval may be resolved into these technical bindings; no fake workbook `Yes` entries are needed. A provenance string or local hash cannot authenticate the underlying human instruction.

Preparation needs fresh source and parent receipts, complete descendant membership where applicable, exact proposed-name collision IDs, ancestry and exclusion checks. Source/parent/collision/membership evidence must be at most 60 seconds old; marking needs evidence captured after preparation. Optional metadata remains absent/null with explicit availability reporting. Supplied known values must match the raw receipt. No invented `canMove=true`, version or empty effective-access list is accepted as fact.

A successful response is still `UNCERTAIN` until independent readback. Terminal evidence for success requires the exact recorded raw response and trusted completion provenance (tool, arguments hash, response hash, call identity and completion time). A timeout or crash without that evidence uses `AGENT_UNRESOLVED` with `terminal=false`; a matching target alone does not clear the unresolved ticket. Error/refusal classification cannot be inferred from a vague failure string. The current bridge conservatively leaves an ambiguous failure unresolved.

To pause future attempts while retaining receipt/reconciliation access:

```sh
python3 -m uqmc_recovery disable-agent-dispatch --controller-id CONTROLLER --reason-code HOLD
```

Setting the private policy hold also blocks authorisation/preparation/marking. Receipts and reconciliation remain available after expiry or a hold. `rollback-plan --plan private/forward.json --selected OPERATION_IDS --batch-id ROLLBACK_BATCH` constructs only a linked candidate from verified journal evidence. A separate scoped rollback approval and fresh handoff checks are still required. Inverse titles restore exact historical preimages, including missing or wrong suffixes; they cannot use an arbitrary `original_operation_id` to bypass naming checks.

## Crash and retry rules

Before `ATTEMPTED`, a fresh recheck may complete preparation. At or after `ATTEMPTED`, do not issue a second ticket, even if no tool invocation is known to have happened. A crash after tool success but before result ingestion is reconciled by stable ID and terminal evidence. A timeout may leave a request running. Neither a local ticket nor a journal lock can prevent a person or external tool from reusing a copied request; the runner must follow this protocol. No exactly-once claim is made.

Cross-process scope reservations and terminal states prevent a new local batch from bypassing an uncertain attempt. A reservation is not a lock on Drive itself. Local files are owner-mutable and hash-chained for tamper evidence, not immutable approval records.

Expired preparations keep their scope reservations. They do not release automatically or issue a replacement ticket. Releasing one safely is a separate reviewed recovery procedure; this version has no automatic cancellation path.

## Provider limits and readiness

Mandatory before a real move: exact source/destination and original parent IDs, exact name/MIME, complete parent relationships, the bounded membership/exclusion check, safe target selection, a current conflict check, exact approval binding, durable evidence and an agreed uncertain-outcome procedure. Conflicting or missing critical identity/parent evidence blocks dispatch.

Provider fields such as version, trash status, owner or capability flags must not be invented when omitted. Their absence must be evaluated for the specific action, rather than called an empty or valid value. Complete effective-access equality is required only when preservation is an approval condition. If the user explicitly accepts changed access, retain available grants, document uncertainty and check newly exposed audiences without adding permission API operations. Reversing a parent relationship cannot promise restored propagation or undo disclosure.

The current connector has no exposed atomic precondition or proven timed-out-request terminality. Two reads cannot eliminate the race after the last read. A production handoff needs a reviewed strategy for that residual race; known or suspected concurrent changes must block. It must fence unknown outcomes without automatic retry. Agent mode implements the bounded strategy: one controller, one unresolved dispatch at a time, fresh evidence and no retry of uncertain calls. Actual exact manifest import, independent review, checkpoint and explicit release of the configured hold remain pending. The process cannot eliminate the remote race or prevent another Drive client from changing the same item.
