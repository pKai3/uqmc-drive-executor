# Frozen destination-section approval

`uqmc_recovery.section_importer.import_section` accepts an explicit owner instruction for a destination section and derives its exact move operations. Blank per-source decision cells do not require new ticks. `No`, excluded and withdrawn records remain excluded. The existing generic workbook importer keeps its original default behavior.

The currently selected method is the explicitly authorized `existing_folder_consolidation` with `target_mode=REUSE_EXISTING_FOLDER`: rename the existing destination root and all section files, move that root into the independently identified target section, retain its existing children, and move the incoming files individually. Reuse is intentional only when its physical identity, old parents, target section and root operation are independently bound. A viewer node source ID alone does not establish that identity. The alternate `new_destination_individual_files` profile remains available when fresh folders are explicitly intended. No real candidate has been validated by these fixture tests.

## API and trust boundary

```python
result = import_section(
    journal, complete_workbook_export,
    projection=frozen_section_projection,
    owner_approval_ref=private_owner_instruction_binding_ref,
    expected_section_id=private_policy["section_id"],
    batch_id="fixture-batch",
    metadata_by_id=complete_verified_metadata,
    resolutions=exact_workbook_resolutions,
    expected_workbook_id=private_policy["workbook_id"],
    review_context_ref=private_policy["review_context_ref"],
    expected_method_profile="existing_folder_consolidation",
    metadata_profile="AGENT_ASSISTED",
)
```

Callers must select the intended method explicitly. The current reuse mode is shown above; the API conservatively retains the earlier new-folder expectation if no override is supplied, so it cannot silently reinterpret an existing plan as reuse. Generic `approved_folder_scope` is a separate independently approved mode. The projection and owner evidence must match the selected profile.

`review_context_ref` is supplied independently by trusted orchestration/private policy, never selected from the projection being tested. Its journal evidence has:

```json
{
  "schema_version": "uqmc.section.review-context.v1",
  "section_id": "fixture-section",
  "model_revision": "corrected-model-v2",
  "source_inventory_evidence_ref": "sha256:...",
  "destination_spec_evidence_ref": "sha256:...",
  "reviewed_by": "fixture-reviewer",
  "reviewed_at_utc": "2026-01-01T00:00:00Z",
  "provenance": "independently_reviewed_corrected_model"
}
```

The referenced source inventory uses `schema_version=uqmc.section.source-inventory.v1`, the same `section_id`, `complete=true`, `source_container_ids_complete=true`, and exact `source_file_ids` and `source_container_ids` arrays. This is an independently grounded file set, not a count or the projection's own membership assertion. The destination specification uses `schema_version=uqmc.section.destination-spec.v1`, matching `section_id` and `model_revision`, `complete=true`, and a `destinations` array of exact logical specifications: `logical_key`, `logical_path`, `folder_name`, `parent_section_id`, `kind=NEW_FOLDER_REQUIRED`. The physical target-section ID must have been grounded independently; a display-node ID is not that evidence.

Hash validation establishes integrity and binding, not truth or the human reviewer's identity. The caller must establish that provenance. A self-authored JSON statement is not independent review. The real review context and corrected live model remain prerequisites, not completed work.

## Projection and owner scope

The projection uses `schema_version=uqmc.destination.section.v1`, matching `section_id`, `model_revision`, `projection_revision`, `workbook_id`, `input_sha256` from the complete workbook export, `complete=true`, `method_profile`, `allowed_actions=["MOVE"]`, and `allowed_destination_parent_ids`. It also includes:

- `declared_member_count`, checked against the exact `members` array; count alone never establishes scope.
- `members`: `{source_id,direct_operation_refs,ancestor_move_operation_refs}` for every source identity.
- `operations`: `{reference,source_id,target_parent_ids,depends_on_refs,target_logical_key}`. References join the workbook manifest; destination IDs and dependency scope must exactly match the independently reviewed resolutions.
- `source_inventory_evidence_ref`, which must equal the independent review context's inventory reference.
- `destination_requirements`, described below.

The alternate new-folder profile requires each independently expected file to have exactly one direct move operation. It forbids source-folder MOVE operations and inherited-member shortcuts. It rejects missing files, extra files, duplicate mappings and dependencies outside the selected scope. Every original parent must appear in the complete source-container inventory; no original/source container or source file can become the destination or target section.

Owner evidence uses `schema_version=uqmc.section.owner-approval.v1`, `decision=APPROVED`, `approval_id`, `actor`, `approved_at_utc`, optional `expires_at_utc`, `provenance={kind:"explicit_owner_instruction",evidence_reference:"opaque trusted instruction reference"}`, matching `section_id` and `method_profile`, `projection_revision`, `projection_sha256`, exact `member_source_ids`, exact `operation_refs`, matching `allowed_actions`, and exact `allowed_destination_parent_ids`. It can list `supersedes_approval_ids`. Revoked or superseded approvals cannot be used.

Trusted orchestration derives the concrete frozen binding from the existing section instruction. The user does not need to approve individual rows or type a hash. If the concrete batch remains within that instruction, binding it is an implementation step, not a request for repeated human permission. A changed section, new source files, different method, destination or withdrawal requires explicit revalidation; it cannot silently expand the previous scope.

## Fresh destination requirements

An unresolved destination is represented without a fabricated physical ID:

```json
{
  "logical_key": "fixture-new-folder",
  "logical_path": "Fixture target/Fresh folder",
  "folder_name": "Fresh folder",
  "parent_section_id": "fixture-target-section-id",
  "kind": "NEW_FOLDER_REQUIRED",
  "status": "UNRESOLVED",
  "created_folder_id": null,
  "creation_evidence_ref": null
}
```

Corresponding operation `target_parent_ids` and the resolved destination boundary are empty until creation is verified. `UNRESOLVED` blocks with `NEW_DESTINATION_CREATION_REQUIRED`. `UNCERTAIN` blocks with `DESTINATION_CREATION_UNCERTAIN_NO_RETRY`; it does not trigger another create call. No CREATE_FOLDER implementation or automatic creation exists here.

Only `status=VERIFIED_CREATED` may supply `created_folder_id` and a `creation_evidence_ref`. The requirement rejects ambiguous identity keys such as `id`, `sourceId`, `source_id`, `folder_id`, `display_node_id` or `target_id`. A real stable ID alone is not enough: it must be the newly returned folder ID with matching provenance, and cannot be any original container.

The creation receipt uses `schema_version=uqmc.folder.creation.receipt.v1`, `state=VERIFIED_CREATED`, `request_terminal=true`, exact logical key/path/name/parent-section/created-folder fields, and private `request_ref`, `response_ref`, `readback_ref`, `creation_authorization_ref`. These references must show the exact CREATE_FOLDER request, returned ID, complete folder readback, and separate authorized creation scope. The creation authorization uses `schema_version=uqmc.folder.creation.authorization.v1`, `decision=APPROVED`, an actor, `owner_instruction_evidence_reference`, and the same logical key/path/name/parent-section fields. Readback must show the expected native folder MIME, exact name, exact parent-section ID and an empty complete subtree. Current destination metadata must match that verified readback. Unknown outcome, missing evidence or subsequent drift blocks import.

## Result and execution boundary

The result is `ImportResult(plan, manifest, evidence_ref)`. Structural or stale approval errors raise `SafetyError`; unresolved scope or rows produce `plan=None` and explicit blocker reasons. A blocked member never silently disappears from the batch. The manifest binds the section projection, owner evidence, independent review context and exact member-to-operation mapping. The plan revision binds this scope together with the workbook snapshot. Private evidence retains the complete inputs; no shared workbook, viewer or source resource is changed.

`section_approval_validated=true` records successful section-scope binding. `execution_approved=false` means the importer has not constructed an executable dispatcher approval; it does not automatically mean another human confirmation is required. The authorized orchestration must bind the exact candidate to its execution gate using the original instruction, complete live preconditions and the current pause/scope constraints. This module creates neither an executable `Approval` nor action events.

The generic folder-scope profile verifies complete descendant membership and records inherited effects through ancestor-operation links, without fictitious child moves. That support does not authorize folder moves for the alternate individual-file profile. The reuse profile separately authorizes one exact supporting root operation.

## Tests and current limitations

Run `python3 -m unittest discover -s tests -p 'test*importer.py' -v`. Synthetic tests cover default legacy behavior, explicit section eligibility, rejected bypass tokens, stale approvals, exclusions, unrelated sections, move-only scope, unknown creation, source-container reuse, display/source identity confusion, independent inventory/spec anchors, folder/inherited substitution, missing metadata/resolutions and destination drift.

This work is code and fixture validation only. It performed no live source reads, candidate preparation, folder creation, moves, renames, permission changes, workbook updates or publishing. The source-to-destination semantic error exposed a real gap: safe IDs and hashes did not establish the intended architecture. The new profile checks the supplied independent anchors, but the actual corrected model, independent review context, complete metadata and verified new-folder provenance are still required before it can yield a real candidate.


## Explicit reuse, historical access and rename ordering

For `existing_folder_consolidation`, both projection and owner evidence include `target_mode=REUSE_EXISTING_FOLDER` and `supporting_container_ids=[<exact reused root ID>]`. Supporting containers are not counted among file members. The independent inventory partitions `source_file_ids` into disjoint `existing_member_source_ids` and `incoming_source_file_ids`; it also binds supporting containers and the complete original-container set. The exact operation count follows those sets, not a count-only assertion: one root MOVE, one MOVE per incoming file, and—when renames are authorized—one RENAME per file plus one root RENAME. Existing children keep their direct parent IDs and have no direct MOVE.

The independently anchored destination spec and projection must contain identical `reuse_target`: `logical_key`, `logical_path`, preimage `folder_name`, `reused_folder_id`, exact `original_parent_ids`, `target_section_id`, `root_move_operation_ref`, and `final_folder_name` when the root is renamed. Root metadata must match the exact preimage and complete existing-member subtree. Every incoming item must be a file from the independently reviewed source-container set. No permission-edit, deletion, content merge or deduplication action is introduced.

The historical exposure instruction is retained as `inherited_access_acknowledgement` in owner evidence: `accepted=true`, the independently reviewed `warning_evidence_ref`, and the exact affected existing-member IDs. The destination spec binds `inherited_access_warning_evidence_ref`. That evidence uses `schema_version=uqmc.section.access-warning.v1`, matching section and target mode, exact `affected_source_ids`, and `shown_to`. This records the user's existing acknowledgement; it does not invent a new permission grant or claim that omitted access metadata is known.

When both MOVE and RENAME are authorized, the independent spec and projection contain the same complete `rename_bindings` list. Every entry binds `source_id`, workbook `reference`, `workbook_row_sha256`, `exact_current_name`, and `exact_target_name`. Root and every file must be represented exactly once. The owner approval binds this complete projection. Blank row decisions are accepted through that section overlay; no cells are changed to Yes.

The required execution graph is explicit: root RENAME first; every file RENAME depends on it; root MOVE depends on **all** section RENAMES; incoming MOVEs depend on the verified root MOVE and their own RENAME. This enforces the rename phase before any move even when operations are prepared individually. The importer updates only planned descendant names in later ancestor subtree preimages. The executor must reconcile those updates against verified dependency events.

The independent destination spec also binds `rename_collision_review_evidence_ref`, whose evidence uses `schema_version=uqmc.section.rename-collision-review.v1`, section/model revision, `complete=true`, exact `rename_bindings_sha256`, and `collisions=[]`. Known current-parent collisions are independently checked against supplied source metadata. A nonempty collision review or known target matching a peer's current title blocks; the importer does not invent temporary names or infer an unreviewed cycle-breaking strategy. Globally unique final file titles alone are insufficient.

Ordinary uploaded extensions are preserved. An explicitly reviewed MIME correction can carry `extension_correction={source_suffix,target_suffix,mime_type,evidence_ref}` in each independent binding, projected operation and resolution. The correction and its exact source/target/MIME evidence are frozen into the operation; the proof must reference existing provenance evidence. This changes a filename suffix only and does not convert content. Native display suffixes remain prohibited.

Duplicate-name exceptions use `collision_policy=PRESERVE_DISTINCT_IDS` and identical independently reviewed `collision_groups` in spec/projection: `{parent_id,exact_name,source_ids}`. Operation `allowed_collision_ids` must exactly equal the appropriate reviewed peer IDs and resolution allowlist. They are frozen into the plan; observed runtime collisions cannot widen the list. Rename operations do not receive move-destination collision exceptions. With complete unique rename targets, these lists should normally be empty.

## Honest agent metadata seam

`metadata_profile=AGENT_ASSISTED` uses `agent_snapshot`, `validate_agent_operation` and `validate_agent_plan` rather than weakening strict snapshot validation. It requires exact ID, exact title, MIME and a complete parent array, explicitly `parents_complete=true`, plus a complete folder subtree. Optional version, capabilities, trash status or grants remain absent/null and are listed as unavailable; no truth values are fabricated. Available grants are retained. The exact candidate can then be sealed by `register_agent_plan` for the agent action bridge. These observations do not establish complete effective access or standalone connector execution.

The legacy/default metadata profile remains STRICT and still rejects incomplete metadata. The agent profile is an explicit operational choice, with its missing-data limits preserved in evidence and the agent bridge's checks. The section importer still neither generates execution approval nor calls Drive. Actual manifests, exact-name audit evidence, independent context and action preconditions must be supplied through the trusted orchestration path before any real preparation.
