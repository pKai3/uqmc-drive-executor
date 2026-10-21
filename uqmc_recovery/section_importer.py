"""Freeze a complete destination-section approval into exact move candidates.

No Drive calls, mutations, per-row ticks, or executable Approval objects.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone

from .importer import ImportResult, WORKBOOK_ID, catalog, import_workbook, destination_route_record
from .model import FOLDER, Plan, SafetyError, digest, snapshot

_SEAL = object()


@dataclass(frozen=True)
class _Eligibility:
    seal: object
    input_sha256: str
    refs: frozenset[str]
    owner_approval_ref: str
    approval_sha256: str
    approval_id: str
    derived_records: tuple = ()


def _not_revoked(journal, approval_id):
    for event in journal.records():
        if event["kind"] in {"REVOKED", "SECTION_APPROVAL_REVOKED", "SECTION_APPROVAL_SUPERSEDED"}:
            if event["payload"].get("approval_id") == approval_id:
                raise SafetyError("SECTION_APPROVAL_REVOKED_OR_SUPERSEDED")


def _eligibility_refs(token, input_hash, selected, journal):
    """Internal importer hook. A list or boolean cannot bypass row eligibility."""
    if not isinstance(token, _Eligibility) or token.seal is not _SEAL:
        raise SafetyError("INVALID_SECTION_ELIGIBILITY")
    if token.input_sha256 != input_hash or token.refs != frozenset(selected):
        raise SafetyError("SECTION_ELIGIBILITY_SCOPE_MISMATCH")
    if digest(journal.read_evidence(token.owner_approval_ref)) != token.approval_sha256:
        raise SafetyError("SECTION_APPROVAL_EVIDENCE_CHANGED")
    _not_revoked(journal, token.approval_id)
    return token.refs


def _eligibility_records(token):
    if not isinstance(token, _Eligibility) or token.seal is not _SEAL:
        raise SafetyError("INVALID_SECTION_ELIGIBILITY")
    return {r["reference"]: copy.deepcopy(r) for r in token.derived_records}


def _destination_catalog(source, projection):
    if projection.get("method_profile") not in {"destination_scoped_files", "destination_hybrid"}:
        return source
    augmented = copy.deepcopy(source)
    for operation in projection.get("operations", []):
        reference = operation.get("reference", "")
        if not isinstance(reference, str) or not reference.startswith("ROUTE/"):
            continue
        origin = source["records"].get(reference[len("ROUTE/"):])
        if origin is None:
            raise SafetyError("DESTINATION_ROUTE_ORIGIN_MISSING")
        derived = destination_route_record(origin)
        if derived["reference"] != reference or derived["source_id"] != operation.get("source_id"):
            raise SafetyError("DESTINATION_ROUTE_ORIGIN_MISMATCH")
        augmented["records"][reference] = derived
    return augmented


def _strings(values, code, *, empty=False):
    if not isinstance(values, list) or (not empty and not values):
        raise SafetyError(code)
    if any(not isinstance(value, str) or not value for value in values) or len(values) != len(set(values)):
        raise SafetyError(code)
    return set(values)


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise SafetyError("SECTION_APPROVAL_TIME_INVALID") from None


def _workbook_rename_target(journal, record, binding, before):
    """Bind the target value, not just the row containing the proposal.

    A known native display suffix may differ only with an explicit, sealed
    conversion record. Uploaded titles and folders always match the cell.
    """
    proposed = record["row"].get("Proposed name + type suffix")
    target = binding.get("exact_target_name")
    if not isinstance(proposed, str) or not proposed:
        raise SafetyError("WORKBOOK_PROPOSED_RENAME_REQUIRED")
    if proposed == target:
        return
    suffix = {
        "application/vnd.google-apps.document": ".gdoc",
        "application/vnd.google-apps.spreadsheet": ".gsheet",
        "application/vnd.google-apps.presentation": ".gslides",
    }.get(before["mime_type"])
    if not suffix or not isinstance(target, str) or not target or proposed != target + suffix:
        raise SafetyError("WORKBOOK_RENAME_TARGET_MISMATCH")
    proof_ref = binding.get("native_display_conversion_ref")
    if not proof_ref:
        raise SafetyError("VERIFIED_NATIVE_DISPLAY_CONVERSION_REQUIRED")
    proof = journal.read_evidence(proof_ref)
    expected = {
        "schema_version": "uqmc.rename.native-display-conversion.v1",
        "source_id": record["source_id"], "mime_type": before["mime_type"],
        "workbook_row_sha256": record["row_sha256"],
        "workbook_proposed_name": proposed, "exact_target_name": target,
        "display_suffix": suffix,
    }
    if any(proof.get(key) != value for key, value in expected.items()) or not proof.get("provenance_ref"):
        raise SafetyError("NATIVE_DISPLAY_CONVERSION_EVIDENCE_MISMATCH")
    journal.read_evidence(proof["provenance_ref"])


def _projection(projection, source, expected_section_id, expected_workbook_id):
    if projection.get("schema_version") != "uqmc.destination.section.v1":
        raise SafetyError("SECTION_SCHEMA_UNSUPPORTED")
    if not expected_section_id or projection.get("section_id") != expected_section_id:
        raise SafetyError("UNRELATED_SECTION")
    if projection.get("workbook_id") != expected_workbook_id or projection.get("input_sha256") != source["input_sha256"]:
        raise SafetyError("SECTION_WORKBOOK_SNAPSHOT_STALE")
    if projection.get("complete") is not True or not projection.get("projection_revision"):
        raise SafetyError("SECTION_SNAPSHOT_INCOMPLETE")
    allowed_actions = _strings(projection.get("allowed_actions"), "SECTION_ACTIONS_REQUIRED")
    if not allowed_actions <= {"MOVE", "RENAME"} or "MOVE" not in allowed_actions:
        raise SafetyError("SECTION_ACTIONS_UNSUPPORTED")
    profile = projection.get("method_profile")
    if profile not in {"approved_folder_scope", "new_destination_individual_files", "existing_folder_consolidation", "approved_nested_section", "destination_scoped_files", "destination_hybrid"}:
        raise SafetyError("EXPLICIT_METHOD_PROFILE_REQUIRED")
    if profile not in {"existing_folder_consolidation", "approved_nested_section", "destination_scoped_files", "destination_hybrid"} and allowed_actions != {"MOVE"}:
        raise SafetyError("SECTION_MOVE_ONLY")
    allow_unresolved = profile == "new_destination_individual_files"
    boundary = _strings(projection.get("allowed_destination_parent_ids"), "SECTION_TARGET_BOUNDARY_REQUIRED", empty=allow_unresolved)
    members, operations = projection.get("members"), projection.get("operations")
    if not isinstance(members, list) or not members or not isinstance(operations, list) or not operations:
        raise SafetyError("SECTION_MEMBERSHIP_REQUIRED")
    if projection.get("declared_member_count") != len(members):
        raise SafetyError("SECTION_MEMBER_COUNT_MISMATCH")
    member_map, op_map = {}, {}
    supporting = _strings(projection.get("supporting_container_ids", []), "SUPPORTING_CONTAINER_SCOPE_INVALID", empty=True)
    if supporting and profile != "existing_folder_consolidation":
        raise SafetyError("SUPPORTING_CONTAINERS_PROFILE_MISMATCH")
    if profile == "existing_folder_consolidation" and (len(supporting) != 1 or projection.get("target_mode") != "REUSE_EXISTING_FOLDER"):
        raise SafetyError("EXPLICIT_REUSE_TARGET_MODE_REQUIRED")
    for member in members:
        source_id = member.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in member_map:
            raise SafetyError("SECTION_SOURCE_DUPLICATE_OR_MISSING")
        direct = _strings(member.get("direct_operation_refs"), "SECTION_MEMBER_MAPPING_INVALID", empty=True)
        inherited = _strings(member.get("ancestor_move_operation_refs"), "SECTION_MEMBER_MAPPING_INVALID", empty=True)
        if not direct and not inherited:
            raise SafetyError("SECTION_MEMBER_UNMAPPED")
        member_map[source_id] = member
    for operation in operations:
        ref, source_id = operation.get("reference"), operation.get("source_id")
        if not isinstance(ref, str) or not ref or ref in op_map or source_id not in set(member_map) | supporting:
            raise SafetyError("SECTION_OPERATION_MAPPING_INVALID")
        record = source["records"].get(ref)
        if not record or record["action"] not in allowed_actions or record["source_id"] != source_id or operation.get("action", record["action"]) != record["action"]:
            raise SafetyError("SECTION_OPERATION_SOURCE_MISMATCH")
        if record["action"] == "MOVE":
            targets = _strings(operation.get("target_parent_ids"), "SECTION_DESTINATION_REQUIRED", empty=allow_unresolved)
            if not targets <= boundary:
                raise SafetyError("SECTION_DESTINATION_OUTSIDE_BOUNDARY")
        elif not isinstance(operation.get("exact_target_name"), str) or not operation["exact_target_name"] or "target_parent_ids" in operation:
            raise SafetyError("SECTION_EXACT_RENAME_TARGET_REQUIRED")
        _strings(operation.get("depends_on_refs"), "SECTION_DEPENDENCIES_REQUIRED", empty=True)
        op_map[ref] = {**operation, "action": record["action"]}
    source_to_refs = {}
    for ref, operation in op_map.items():
        source_to_refs.setdefault(operation["source_id"], set()).add(ref)
        if not set(operation["depends_on_refs"]) <= set(op_map):
            raise SafetyError("SECTION_DEPENDENCY_OUTSIDE_SCOPE")
    for source_id, member in member_map.items():
        if set(member["direct_operation_refs"]) != source_to_refs.get(source_id, set()):
            raise SafetyError("SECTION_DIRECT_MAPPING_MISMATCH")
        if not set(member["ancestor_move_operation_refs"]) <= set(op_map):
            raise SafetyError("SECTION_ANCESTOR_OUTSIDE_SCOPE")
        if any(op_map[ref]["action"] != "MOVE" for ref in member["ancestor_move_operation_refs"]):
            raise SafetyError("SECTION_ANCESTOR_MUST_BE_MOVE")
    return member_map, op_map, boundary


def _approval(journal, approval_ref, projection, member_map, op_map, boundary):
    approval = journal.read_evidence(approval_ref)
    if approval.get("schema_version") != "uqmc.section.owner-approval.v1" or approval.get("decision") != "APPROVED":
        raise SafetyError("EXPLICIT_SECTION_APPROVAL_REQUIRED")
    if not approval.get("approval_id") or not approval.get("actor"):
        raise SafetyError("SECTION_APPROVAL_IDENTITY_REQUIRED")
    provenance = approval.get("provenance", {})
    if provenance.get("kind") != "explicit_owner_instruction" or not provenance.get("evidence_reference"):
        raise SafetyError("SECTION_OWNER_INSTRUCTION_EVIDENCE_REQUIRED")
    if approval.get("section_id") != projection["section_id"]:
        raise SafetyError("UNRELATED_SECTION_APPROVAL")
    if approval.get("projection_revision") != projection["projection_revision"] or approval.get("projection_sha256") != digest(projection):
        raise SafetyError("SECTION_APPROVAL_STALE")
    if approval.get("method_profile") != projection["method_profile"]:
        raise SafetyError("SECTION_APPROVAL_METHOD_MISMATCH")
    if projection["method_profile"] == "existing_folder_consolidation":
        if approval.get("target_mode") != "REUSE_EXISTING_FOLDER" or approval.get("supporting_container_ids") != projection.get("supporting_container_ids"):
            raise SafetyError("SECTION_REUSE_APPROVAL_SCOPE_MISMATCH")
    if approval.get("allowed_actions") != projection["allowed_actions"]:
        raise SafetyError("SECTION_ACTION_APPROVAL_MISMATCH")
    if _strings(approval.get("member_source_ids"), "SECTION_APPROVAL_MEMBERS_INVALID") != set(member_map):
        raise SafetyError("SECTION_APPROVAL_MEMBER_SCOPE_MISMATCH")
    if _strings(approval.get("operation_refs"), "SECTION_APPROVAL_OPERATIONS_INVALID") != set(op_map):
        raise SafetyError("SECTION_APPROVAL_OPERATION_SCOPE_MISMATCH")
    if _strings(approval.get("allowed_destination_parent_ids"), "SECTION_APPROVAL_DESTINATIONS_INVALID", empty=projection["method_profile"] == "new_destination_individual_files") != boundary:
        raise SafetyError("SECTION_APPROVAL_TARGET_SCOPE_MISMATCH")
    current = datetime.now(timezone.utc)
    if _time(approval.get("approved_at_utc")) > current:
        raise SafetyError("SECTION_APPROVAL_IN_FUTURE")
    if approval.get("expires_at_utc") is not None and _time(approval["expires_at_utc"]) <= current:
        raise SafetyError("SECTION_APPROVAL_EXPIRED")
    _not_revoked(journal, approval["approval_id"])
    return approval


def validate_section_authority(journal, plan):
    """Recheck frozen section authority without reading Drive or editable cells.

    Call under the action gate's journal lock at authorization and again before
    preparation/dispatch. An ordinary non-section fixture plan returns None.
    Section plans must retain their exact recorded import origin.
    """
    origins = [event for event in journal.records() if event["kind"] == "SECTION_IMPORTED"
               and event["payload"].get("proposal_hash") == plan.proposal_hash]
    if not origins:
        if any(event["kind"] == "BATCH_SOURCE_BOUND" and event["payload"].get("proposal_hash") == plan.proposal_hash
               for event in journal.records()):
            from .batch_source import refreshed_section_authority
            return refreshed_section_authority(journal, plan)
        if plan.proposal_revision.startswith("section:"):
            raise SafetyError("SECTION_PLAN_ORIGIN_REQUIRED")
        return None
    origin = origins[-1]
    payload = origin["payload"]
    if payload.get("run_id") != plan.run_id or payload.get("batch_id") != plan.batch_id:
        raise SafetyError("SECTION_PLAN_ORIGIN_MISMATCH")
    evidence = journal.read_evidence(payload["manifest_ref"])
    manifest, projection = evidence["manifest"], evidence["projection"]
    binding = manifest.get("authorization_scope", {})
    owner_ref = evidence.get("owner_approval_ref")
    if (digest(evidence.get("plan")) != plan.proposal_hash
            or manifest.get("schema_version") != "uqmc.section.import.v1"
            or manifest.get("section_approval_validated") is not True
            or manifest.get("proposal_hash") != plan.proposal_hash
            or manifest.get("proposal_revision") != plan.proposal_revision
            or binding.get("owner_approval_ref") != owner_ref
            or payload.get("owner_approval_ref") != owner_ref
            or binding.get("projection_sha256") != digest(projection)
            or payload.get("projection_sha256") != digest(projection)):
        raise SafetyError("SECTION_PLAN_ORIGIN_MISMATCH")
    workbook_evidence = journal.read_evidence(evidence["workbook_import_ref"])
    workbook_manifest = workbook_evidence["manifest"]
    source = _destination_catalog(catalog(workbook_evidence["workbook_export"], expected_workbook_id=manifest["workbook_id"]), projection)
    members, operations, boundary = _projection(projection, source, binding.get("section_id"), manifest["workbook_id"])
    approval = _approval(journal, owner_ref, projection, members, operations, boundary)
    expected_revision = "section:" + digest({"scope": binding, "workbook_revision": workbook_manifest["proposal_revision"]})
    if expected_revision != plan.proposal_revision or binding.get("owner_approval_id") != approval["approval_id"]:
        raise SafetyError("SECTION_PLAN_ORIGIN_MISMATCH")
    return {"schema_version": "uqmc.section.authority-binding.v1",
            "section_import_event_id": origin["event_id"], "section_import_event_seq": origin["seq"],
            "section_manifest_ref": payload["manifest_ref"], "section_id": projection["section_id"],
            "owner_approval_ref": owner_ref, "owner_approval_id": approval["approval_id"],
            "projection_sha256": digest(projection), "proposal_hash": plan.proposal_hash,
            "proposal_revision": plan.proposal_revision, "expires_at_utc": approval.get("expires_at_utc")}


def _individual_profile(journal, projection, members, operations, boundary, metadata_by_id, review_context_ref):
    """Validate method semantics, not merely counts or opaque stable IDs."""
    if not review_context_ref:
        raise SafetyError("INDEPENDENT_MODEL_REVIEW_CONTEXT_REQUIRED")
    context = journal.read_evidence(review_context_ref)
    if context.get("schema_version") != "uqmc.section.review-context.v1" or context.get("provenance") != "independently_reviewed_corrected_model":
        raise SafetyError("INDEPENDENT_MODEL_REVIEW_CONTEXT_INVALID")
    if context.get("section_id") != projection["section_id"] or not context.get("model_revision") or context.get("model_revision") != projection.get("model_revision"):
        raise SafetyError("INDEPENDENT_MODEL_REVIEW_SCOPE_MISMATCH")
    if not context.get("reviewed_by") or _time(context.get("reviewed_at_utc")) > datetime.now(timezone.utc):
        raise SafetyError("INDEPENDENT_MODEL_REVIEW_IDENTITY_REQUIRED")
    if projection.get("source_inventory_evidence_ref") != context.get("source_inventory_evidence_ref"):
        raise SafetyError("INDEPENDENT_SOURCE_INVENTORY_BINDING_MISMATCH")
    spec = journal.read_evidence(context.get("destination_spec_evidence_ref"))
    if spec.get("schema_version") != "uqmc.section.destination-spec.v1" or spec.get("section_id") != projection["section_id"] or spec.get("model_revision") != context["model_revision"] or spec.get("complete") is not True:
        raise SafetyError("INDEPENDENT_DESTINATION_SPEC_REQUIRED")
    expected_destinations = spec.get("destinations")
    if not isinstance(expected_destinations, list) or not expected_destinations:
        raise SafetyError("INDEPENDENT_DESTINATION_SPEC_REQUIRED")
    logical_fields = ("logical_key", "logical_path", "folder_name", "parent_section_id", "kind")
    expected_specs = [{key: item.get(key) for key in logical_fields} for item in expected_destinations]
    actual_specs = [{key: item.get(key) for key in logical_fields} for item in projection.get("destination_requirements", [])]
    if sorted(expected_specs, key=lambda item: str(item["logical_key"])) != sorted(actual_specs, key=lambda item: str(item["logical_key"])):
        raise SafetyError("INDEPENDENT_DESTINATION_SPEC_BINDING_MISMATCH")
    inventory = journal.read_evidence(projection.get("source_inventory_evidence_ref"))
    if inventory.get("schema_version") != "uqmc.section.source-inventory.v1" or inventory.get("section_id") != projection["section_id"]:
        raise SafetyError("INDEPENDENT_SOURCE_INVENTORY_REQUIRED")
    if inventory.get("complete") is not True or inventory.get("source_container_ids_complete") is not True:
        raise SafetyError("INDEPENDENT_SOURCE_INVENTORY_INCOMPLETE")
    expected_files = _strings(inventory.get("source_file_ids"), "EXPECTED_SOURCE_FILE_SET_REQUIRED")
    containers = _strings(inventory.get("source_container_ids"), "ORIGINAL_CONTAINER_SET_REQUIRED")
    if set(members) != expected_files or {op["source_id"] for op in operations.values()} != expected_files or len(operations) != len(expected_files):
        raise SafetyError("INDIVIDUAL_FILE_SET_MISMATCH")
    if any(member["ancestor_move_operation_refs"] or len(member["direct_operation_refs"]) != 1 for member in members.values()):
        raise SafetyError("INDIVIDUAL_FILES_FORBID_INHERITED_SHORTCUTS")
    requirements = projection.get("destination_requirements")
    if not isinstance(requirements, list) or not requirements:
        raise SafetyError("NEW_DESTINATION_REQUIREMENTS_MISSING")
    requirements_by_key, resolved_ids, blocked = {}, set(), None
    for requirement in requirements:
        key = requirement.get("logical_key")
        if not isinstance(key, str) or not key or key in requirements_by_key:
            raise SafetyError("DESTINATION_LOGICAL_KEY_INVALID")
        if any(not isinstance(requirement.get(field), str) or not requirement[field] for field in ("logical_path", "folder_name", "parent_section_id")):
            raise SafetyError("DESTINATION_LOGICAL_BINDING_REQUIRED")
        if any(key in requirement for key in ("id", "sourceId", "source_id", "folder_id", "display_node_id", "target_id")):
            raise SafetyError("DISPLAY_OR_SOURCE_ID_IS_NOT_DESTINATION_ID")
        if requirement.get("kind") != "NEW_FOLDER_REQUIRED":
            raise SafetyError("FRESH_DESTINATION_REQUIRED")
        status = requirement.get("status")
        if status not in {"UNRESOLVED", "UNCERTAIN", "VERIFIED_CREATED"}:
            raise SafetyError("DESTINATION_STATUS_INVALID")
        if requirement["parent_section_id"] in containers | expected_files:
            raise SafetyError("SOURCE_CONTAINER_REUSED_AS_TARGET_SECTION")
        if status != "VERIFIED_CREATED":
            if requirement.get("created_folder_id") is not None or requirement.get("creation_evidence_ref") is not None:
                raise SafetyError("UNRESOLVED_DESTINATION_MUST_NOT_INVENT_ID")
            blocked = "DESTINATION_CREATION_UNCERTAIN_NO_RETRY" if status == "UNCERTAIN" else "NEW_DESTINATION_CREATION_REQUIRED"
        else:
            folder_id = requirement.get("created_folder_id")
            if not isinstance(folder_id, str) or not folder_id or folder_id in resolved_ids:
                raise SafetyError("DESTINATION_FOLDER_ID_INVALID")
            if folder_id in containers | expected_files or folder_id == requirement["parent_section_id"]:
                raise SafetyError("ORIGINAL_CONTAINER_REUSED_AS_DESTINATION")
            resolved_ids.add(folder_id)
        requirements_by_key[key] = requirement
    if resolved_ids != boundary:
        raise SafetyError("DESTINATION_BOUNDARY_NOT_EXACT")
    if {op.get("target_logical_key") for op in operations.values()} != set(requirements_by_key):
        raise SafetyError("OPERATION_LOGICAL_DESTINATION_MISMATCH")
    for operation in operations.values():
        requirement = requirements_by_key[operation["target_logical_key"]]
        expected_ids = [requirement["created_folder_id"]] if requirement["status"] == "VERIFIED_CREATED" else []
        if operation["target_parent_ids"] != expected_ids:
            raise SafetyError("LOGICAL_DESTINATION_ID_BINDING_MISMATCH")
    if blocked:
        return blocked
    # Validate complete source snapshots only after unresolved creation is ruled out.
    for source_id in expected_files:
        before = snapshot(metadata_by_id[source_id])
        if before["id"] != source_id or before["mime_type"] == FOLDER:
            raise SafetyError("INDIVIDUAL_FILES_FORBID_SOURCE_FOLDER_MOVES")
        if not set(before["parents"]) <= containers:
            raise SafetyError("ORIGINAL_CONTAINER_INVENTORY_MISMATCH")
        if set(before["parents"]) & boundary:
            raise SafetyError("ORIGINAL_PARENT_REUSED_AS_DESTINATION")
    for requirement in requirements:
        receipt = journal.read_evidence(requirement.get("creation_evidence_ref"))
        if receipt.get("schema_version") != "uqmc.folder.creation.receipt.v1" or receipt.get("state") != "VERIFIED_CREATED" or receipt.get("request_terminal") is not True:
            raise SafetyError("VERIFIED_NEW_FOLDER_RECEIPT_REQUIRED")
        if any(receipt.get(key) != requirement[key] for key in ("logical_key", "logical_path", "folder_name", "parent_section_id", "created_folder_id")):
            raise SafetyError("CREATION_RECEIPT_LOGICAL_BINDING_MISMATCH")
        request = journal.read_evidence(receipt.get("request_ref"))
        response = journal.read_evidence(receipt.get("response_ref"))
        readback = snapshot(journal.read_evidence(receipt.get("readback_ref")))
        authority = journal.read_evidence(receipt.get("creation_authorization_ref"))
        if authority.get("schema_version") != "uqmc.folder.creation.authorization.v1" or authority.get("decision") != "APPROVED" or not authority.get("actor") or not authority.get("owner_instruction_evidence_reference"):
            raise SafetyError("EXPLICIT_FOLDER_CREATION_AUTHORIZATION_REQUIRED")
        for key in ("logical_key", "logical_path", "folder_name", "parent_section_id"):
            if authority.get(key) != requirement[key] or request.get(key) != requirement[key]:
                raise SafetyError("CREATION_REQUEST_SCOPE_MISMATCH")
        if request.get("action") != "CREATE_FOLDER" or response.get("id") != requirement["created_folder_id"]:
            raise SafetyError("CREATION_RESPONSE_ID_MISMATCH")
        if readback["id"] != requirement["created_folder_id"] or readback["mime_type"] != FOLDER or readback["name"] != requirement["folder_name"] or readback["parents"] != [requirement["parent_section_id"]]:
            raise SafetyError("CREATION_READBACK_MISMATCH")
        if readback["subtree"].get("members"):
            raise SafetyError("NEW_DESTINATION_NOT_EMPTY_AT_CREATION")
        current = snapshot(metadata_by_id[requirement["created_folder_id"]])
        if current != readback:
            raise SafetyError("NEW_DESTINATION_CHANGED_SINCE_VERIFICATION")
    return None


def _blocked(journal, projection, approval_ref, approval, batch_id, reason, review_context_ref=None):
    manifest = {"schema_version": "uqmc.section.import.v1", "dry_run": True, "batch_id": batch_id,
                "section_approval_validated": True, "execution_approved": False,
                "plan_ready_for_exact_batch_binding": False, "proposal_hash": None,
                "section_reason_code": reason,
                "authorization_scope": {"section_id": projection["section_id"], "method_profile": projection["method_profile"],
                    "projection_revision": projection["projection_revision"], "projection_sha256": digest(projection),
                    "owner_approval_ref": approval_ref, "owner_approval_id": approval["approval_id"], "review_context_ref": review_context_ref, "batch_binding_required": True},
                "rows": [{"reference": op["reference"], "source_id": op["source_id"], "action": op.get("action", "MOVE"), "state": "BLOCKED", "reason_code": reason} for op in projection["operations"]]}
    ref = journal.evidence({"projection": projection, "owner_approval_ref": approval_ref, "manifest": manifest, "plan": None})
    journal.append("SECTION_IMPORT_BLOCKED", payload={"run_id": batch_id, "manifest_ref": ref, "reason_code": reason, "dry_run": True})
    return ImportResult(None, manifest, ref)


def _reuse_profile(journal, projection, members, operations, boundary, metadata_by_id, review_context_ref, approval, source, normalize=snapshot):
    if not review_context_ref:
        raise SafetyError("INDEPENDENT_MODEL_REVIEW_CONTEXT_REQUIRED")
    context = journal.read_evidence(review_context_ref)
    if context.get("schema_version") != "uqmc.section.review-context.v1" or context.get("provenance") != "independently_reviewed_corrected_model" or not context.get("reviewed_by"):
        raise SafetyError("INDEPENDENT_MODEL_REVIEW_CONTEXT_INVALID")
    if context.get("section_id") != projection["section_id"] or context.get("model_revision") != projection.get("model_revision") or not context.get("model_revision"):
        raise SafetyError("INDEPENDENT_MODEL_REVIEW_SCOPE_MISMATCH")
    if _time(context.get("reviewed_at_utc")) > datetime.now(timezone.utc):
        raise SafetyError("INDEPENDENT_MODEL_REVIEW_IN_FUTURE")
    if context.get("source_inventory_evidence_ref") != projection.get("source_inventory_evidence_ref"):
        raise SafetyError("INDEPENDENT_SOURCE_INVENTORY_BINDING_MISMATCH")
    inventory = journal.read_evidence(context["source_inventory_evidence_ref"])
    spec = journal.read_evidence(context.get("destination_spec_evidence_ref"))
    if inventory.get("schema_version") != "uqmc.section.source-inventory.v1" or inventory.get("section_id") != projection["section_id"] or inventory.get("complete") is not True or inventory.get("source_container_ids_complete") is not True:
        raise SafetyError("INDEPENDENT_SOURCE_INVENTORY_INCOMPLETE")
    if spec.get("schema_version") != "uqmc.section.destination-spec.v1" or spec.get("section_id") != projection["section_id"] or spec.get("model_revision") != context["model_revision"] or spec.get("complete") is not True or spec.get("target_mode") != "REUSE_EXISTING_FOLDER":
        raise SafetyError("INDEPENDENT_REUSE_DESTINATION_SPEC_REQUIRED")
    target = projection.get("reuse_target")
    if not isinstance(target, dict) or target != spec.get("reuse_target"):
        raise SafetyError("INDEPENDENT_REUSE_TARGET_BINDING_MISMATCH")
    for key in ("logical_key", "logical_path", "folder_name", "reused_folder_id", "target_section_id", "root_move_operation_ref"):
        if not isinstance(target.get(key), str) or not target[key]:
            raise SafetyError("EXACT_REUSE_TARGET_REQUIRED")
    old_parents = _strings(target.get("original_parent_ids"), "REUSE_ORIGINAL_PARENTS_REQUIRED")
    root_id, root_ref = target["reused_folder_id"], target["root_move_operation_ref"]
    supporting = _strings(projection.get("supporting_container_ids"), "SUPPORTING_CONTAINER_SCOPE_INVALID")
    if supporting != {root_id} or supporting != _strings(inventory.get("supporting_container_ids"), "INDEPENDENT_SUPPORTING_CONTAINER_REQUIRED"):
        raise SafetyError("REUSE_SUPPORTING_CONTAINER_MISMATCH")
    existing = _strings(inventory.get("existing_member_source_ids"), "EXISTING_MEMBER_SET_REQUIRED", empty=True)
    incoming = _strings(inventory.get("incoming_source_file_ids"), "INCOMING_FILE_SET_REQUIRED")
    expected = _strings(inventory.get("source_file_ids"), "EXPECTED_SOURCE_FILE_SET_REQUIRED")
    containers = _strings(inventory.get("source_container_ids"), "ORIGINAL_CONTAINER_SET_REQUIRED")
    if existing & incoming or expected != existing | incoming or set(members) != expected or root_id in expected:
        raise SafetyError("REUSE_EXACT_MEMBER_PARTITION_MISMATCH")
    acknowledgement = approval.get("inherited_access_acknowledgement", {})
    warning_ref = spec.get("inherited_access_warning_evidence_ref")
    if acknowledgement.get("accepted") is not True or not warning_ref or acknowledgement.get("warning_evidence_ref") != warning_ref or set(acknowledgement.get("affected_source_ids", [])) != existing:
        raise SafetyError("REUSE_INHERITED_ACCESS_ACKNOWLEDGEMENT_REQUIRED")
    warning = journal.read_evidence(warning_ref)
    if warning.get("schema_version") != "uqmc.section.access-warning.v1" or warning.get("section_id") != projection["section_id"] or warning.get("target_mode") != "REUSE_EXISTING_FOLDER" or set(warning.get("affected_source_ids", [])) != existing or not warning.get("shown_to"):
        raise SafetyError("REUSE_ACCESS_WARNING_SCOPE_MISMATCH")
    moves = {ref: op for ref, op in operations.items() if op["action"] == "MOVE"}
    renames = {ref: op for ref, op in operations.items() if op["action"] == "RENAME"}
    if len(moves) != len(incoming) + 1 or {op["source_id"] for op in moves.values()} != incoming | {root_id}:
        raise SafetyError("REUSE_OPERATION_SET_MISMATCH")
    bindings = spec.get("rename_bindings", [])
    if projection.get("rename_bindings", []) != bindings or not isinstance(bindings, list):
        raise SafetyError("INDEPENDENT_RENAME_BINDING_MISMATCH")
    rename_by_source, rename_refs = {}, set()
    for binding in bindings:
        ref, source_id = binding.get("reference"), binding.get("source_id")
        if not isinstance(ref, str) or ref in rename_refs or source_id in rename_by_source or source_id not in expected | {root_id}:
            raise SafetyError("RENAME_BINDING_SOURCE_OR_REFERENCE_INVALID")
        rename_refs.add(ref)
        rename_by_source[source_id] = binding
        record = source["records"].get(ref)
        operation = renames.get(ref)
        if not record or record["action"] != "RENAME" or record["source_id"] != source_id or record["row_sha256"] != binding.get("workbook_row_sha256") or not operation:
            raise SafetyError("EXACT_WORKBOOK_RENAME_BINDING_REQUIRED")
        if operation["source_id"] != source_id or operation["exact_target_name"] != binding.get("exact_target_name"):
            raise SafetyError("RENAME_TARGET_BINDING_MISMATCH")
        if operation.get("extension_correction") != binding.get("extension_correction"):
            raise SafetyError("RENAME_EXTENSION_CORRECTION_BINDING_MISMATCH")
        before = normalize(metadata_by_id[source_id])
        if before["name"] != binding.get("exact_current_name"):
            raise SafetyError("RENAME_CURRENT_TITLE_DRIFT")
        _workbook_rename_target(journal, record, binding, before)
    if "RENAME" in projection["allowed_actions"]:
        if set(rename_by_source) != expected | {root_id} or set(renames) != rename_refs:
            raise SafetyError("EXACT_COMPLETE_RENAME_SCOPE_REQUIRED")
        root_rename_ref = rename_by_source[root_id]["reference"]
        if renames[root_rename_ref]["depends_on_refs"]:
            raise SafetyError("ROOT_RENAME_MUST_PRECEDE_FILE_RENAMES")
        for source_id in expected:
            if root_rename_ref not in renames[rename_by_source[source_id]["reference"]]["depends_on_refs"]:
                raise SafetyError("FILE_RENAME_MUST_FOLLOW_ROOT_RENAME")
        if target.get("final_folder_name") != rename_by_source[root_id]["exact_target_name"]:
            raise SafetyError("FINAL_ROOT_TITLE_LOGICAL_BINDING_MISMATCH")
        review = journal.read_evidence(spec.get("rename_collision_review_evidence_ref"))
        if review.get("schema_version") != "uqmc.section.rename-collision-review.v1" or review.get("section_id") != projection["section_id"] or review.get("model_revision") != context["model_revision"] or review.get("complete") is not True or review.get("rename_bindings_sha256") != digest(bindings):
            raise SafetyError("SOURCE_PARENT_RENAME_COLLISION_REVIEW_REQUIRED")
        if review.get("collisions") != []:
            raise SafetyError("RENAME_COLLISION_REQUIRES_REVIEWED_ORDER_OR_STRATEGY")
        known = {source_id: normalize(metadata_by_id[source_id]) for source_id in expected | {root_id}}
        for source_id, binding in rename_by_source.items():
            if any(other_id != source_id and set(known[source_id]["parents"]) & set(other["parents"]) and binding["exact_target_name"] == other["name"] for other_id, other in known.items()):
                raise SafetyError("SOURCE_PARENT_RENAME_COLLISION_DETECTED")
        final_titles = [rename_by_source[source_id]["exact_target_name"] for source_id in expected]
        if len(final_titles) != len(set(final_titles)):
            raise SafetyError("FINAL_SECTION_FILE_TITLES_NOT_UNIQUE")
    elif renames or bindings:
        raise SafetyError("RENAMES_NOT_AUTHORIZED")
    if boundary != {root_id, target["target_section_id"]} or root_id == target["target_section_id"]:
        raise SafetyError("REUSE_DESTINATION_BOUNDARY_MISMATCH")
    root_op = moves.get(root_ref)
    if not root_op or root_op["source_id"] != root_id or root_op["target_parent_ids"] != [target["target_section_id"]]:
        raise SafetyError("REUSE_ROOT_MOVE_MISMATCH")
    if renames:
        required = {rename_by_source[source_id]["reference"] for source_id in expected | {root_id}}
        if not required <= set(root_op["depends_on_refs"]):
            raise SafetyError("ROOT_MOVE_MUST_WAIT_FOR_ALL_SECTION_RENAMES")
    elif root_op["depends_on_refs"]:
        raise SafetyError("REUSE_ROOT_MOVE_MISMATCH")
    file_to_ref = {op["source_id"]: ref for ref, op in moves.items() if ref != root_ref}
    for source_id, member in members.items():
        if source_id in existing:
            expected_direct = {rename_by_source[source_id]["reference"]} if renames else set()
            if set(member["direct_operation_refs"]) != expected_direct or member["ancestor_move_operation_refs"] != [root_ref]:
                raise SafetyError("EXISTING_MEMBERS_MUST_RETAIN_DIRECT_PARENTS")
        else:
            ref = file_to_ref[source_id]
            op = moves[ref]
            expected_direct = {ref, rename_by_source[source_id]["reference"]} if renames else {ref}
            if set(member["direct_operation_refs"]) != expected_direct or member["ancestor_move_operation_refs"]:
                raise SafetyError("INCOMING_MEMBERS_REQUIRE_DIRECT_MOVE")
            if op["target_parent_ids"] != [root_id] or root_ref not in op["depends_on_refs"] or op.get("target_logical_key") != target["logical_key"]:
                raise SafetyError("INCOMING_TARGET_OR_DEPENDENCY_MISMATCH")
            if renames and rename_by_source[source_id]["reference"] not in op["depends_on_refs"]:
                raise SafetyError("INCOMING_MOVE_MUST_WAIT_FOR_OWN_RENAME")
    root = normalize(metadata_by_id[root_id])
    if root["id"] != root_id or root["mime_type"] != FOLDER or root["name"] != target["folder_name"] or set(root["parents"]) != old_parents:
        raise SafetyError("REUSE_ROOT_PREIMAGE_MISMATCH")
    descendants = root.get("subtree", {})
    if descendants.get("complete") is not True or {item["id"] for item in descendants.get("members", [])} != existing:
        raise SafetyError("REUSE_EXISTING_MEMBER_SCOPE_INCOMPLETE")
    for source_id in existing:
        state = normalize(metadata_by_id[source_id])
        if state["id"] != source_id or state["mime_type"] == FOLDER or root_id not in state["parents"]:
            raise SafetyError("REUSE_EXISTING_MEMBER_NOT_FILE_UNDER_ROOT")
    target_section = normalize(metadata_by_id[target["target_section_id"]])
    if target_section["id"] != target["target_section_id"] or target_section["mime_type"] != FOLDER or target["target_section_id"] in existing:
        raise SafetyError("REUSE_TARGET_SECTION_INVALID")
    for source_id in incoming:
        state = normalize(metadata_by_id[source_id])
        if state["id"] != source_id or state["mime_type"] == FOLDER or not set(state["parents"]) <= containers or root_id in state["parents"]:
            raise SafetyError("REUSE_INCOMING_FILE_PREIMAGE_MISMATCH")
    # Duplicate names are permitted only by the independently reviewed exact-ID
    # groups; no overwriting, content merge, deduplication or renaming is implied.
    groups = spec.get("collision_groups", [])
    if projection.get("collision_groups", []) != groups or projection.get("collision_policy") != "PRESERVE_DISTINCT_IDS" or spec.get("collision_policy") != "PRESERVE_DISTINCT_IDS":
        raise SafetyError("REUSE_COLLISION_POLICY_MISMATCH")
    allowed_by_file = {source_id: set() for source_id in incoming}
    if not isinstance(groups, list):
        raise SafetyError("REUSE_COLLISION_GROUP_INVALID")
    for group in groups:
        ids = _strings(group.get("source_ids"), "REUSE_COLLISION_GROUP_INVALID")
        if len(ids) < 2 or not ids <= expected or group.get("parent_id") != root_id or not isinstance(group.get("exact_name"), str):
            raise SafetyError("REUSE_COLLISION_GROUP_INVALID")
        for source_id in ids & incoming:
            final_name = rename_by_source[source_id]["exact_target_name"] if renames else normalize(metadata_by_id[source_id])["name"]
            if final_name != group["exact_name"]:
                raise SafetyError("REUSE_COLLISION_NAME_MISMATCH")
            allowed_by_file[source_id] |= ids - {source_id}
    for ref, operation in operations.items():
        allowed = _strings(operation.get("allowed_collision_ids", []), "REUSE_COLLISION_ALLOWLIST_INVALID", empty=True)
        expected_allowed = allowed_by_file.get(operation["source_id"], set()) if operation["action"] == "MOVE" else set()
        if allowed != expected_allowed:
            raise SafetyError("REUSE_COLLISION_ALLOWLIST_MISMATCH")
    return None


def _nested_profile(journal, projection, members, operations, boundary, metadata_by_id, source, normalize):
    """Bind nested final folder identities to exact workbook paths.

    The path evidence records deterministic preparation provenance; it is not
    authority. Owner approval seals the entire projection, and complete source
    snapshots below prove every indirect descendant remains inside that scope.
    """
    section_path = projection.get("section_path")
    paths = projection.get("destination_paths")
    if not isinstance(section_path, str) or not section_path or "/" not in section_path:
        raise SafetyError("NESTED_SECTION_PATH_REQUIRED")
    anchor = section_path.rsplit("/", 1)[0]
    if not isinstance(paths, dict) or section_path not in paths or anchor not in paths:
        raise SafetyError("NESTED_DESTINATION_PATHS_REQUIRED")
    if any(not isinstance(path, str) or not path or not isinstance(folder_id, str) or not folder_id
           for path, folder_id in paths.items()) or len(set(paths.values())) != len(paths):
        raise SafetyError("NESTED_DESTINATION_PATH_AMBIGUOUS")
    if any(path != anchor and path != section_path and not path.startswith(section_path + "/") for path in paths):
        raise SafetyError("NESTED_DESTINATION_OUTSIDE_SECTION")
    proof_ref = projection.get("destination_path_evidence_ref")
    if not isinstance(proof_ref, str) or not proof_ref:
        raise SafetyError("NESTED_DESTINATION_EVIDENCE_REQUIRED")
    proof = journal.read_evidence(proof_ref)
    if (proof.get("schema_version") != "uqmc.section.destination-paths.v1"
            or proof.get("section_id") != projection["section_id"]
            or proof.get("input_sha256") != source["input_sha256"]
            or proof.get("destination_paths") != paths or not proof.get("provenance_ref")):
        raise SafetyError("NESTED_DESTINATION_EVIDENCE_MISMATCH")
    journal.read_evidence(proof["provenance_ref"])
    # Use the same deterministic selector, including a renamed section root
    # and explicit exclusions, as preparation. The selected ref set is sealed.
    from .section_prepare import select_records
    selected = {record["reference"] for record in select_records(source, section_path)}
    if set(operations) != selected:
        raise SafetyError("NESTED_SECTION_OPERATION_SET_MISMATCH")
    by_source = {}
    for ref, operation in operations.items():
        group = by_source.setdefault(operation["source_id"], {})
        if operation["action"] in group:
            raise SafetyError("NESTED_DUPLICATE_SOURCE_EFFECT")
        group[operation["action"]] = operation
    states = {source_id: normalize(metadata_by_id[source_id]) for source_id in by_source}
    for source_id, state in states.items():
        if state["id"] != source_id:
            raise SafetyError("ID_MISMATCH")
    expected_boundary = set()
    for ref, operation in operations.items():
        record = source["records"][ref]
        state = states[operation["source_id"]]
        if operation["action"] == "MOVE":
            full_path = record["row"].get("Exact target path")
            if not isinstance(full_path, str) or "/" not in full_path:
                raise SafetyError("NESTED_MOVE_PATH_REQUIRED")
            parent_path = full_path.rsplit("/", 1)[0]
            if parent_path not in paths or operation["target_parent_ids"] != [paths[parent_path]]:
                raise SafetyError("NESTED_MOVE_TARGET_PATH_MISMATCH")
            expected_boundary.add(paths[parent_path])
            own_rename = by_source[operation["source_id"]].get("RENAME")
            if own_rename and own_rename["reference"] not in operation["depends_on_refs"]:
                raise SafetyError("NESTED_MOVE_REQUIRES_OWN_RENAME_DEPENDENCY")
        else:
            _workbook_rename_target(journal, record, operation, state)
            parent_path = record["row"].get("Proposed destination folder")
            if parent_path not in paths:
                raise SafetyError("NESTED_RENAME_DESTINATION_UNRESOLVED")
            move = by_source[operation["source_id"]].get("MOVE")
            final_parents = move["target_parent_ids"] if move else state["parents"]
            if final_parents != [paths[parent_path]]:
                raise SafetyError("NESTED_RENAME_DESTINATION_MISMATCH")
    if boundary != expected_boundary:
        raise SafetyError("NESTED_DESTINATION_BOUNDARY_NOT_EXACT")
    # Path lookup is never allowed to reinterpret a source ID as a destination
    # simply because a display node carried that ID. Its actual final folder
    # name and parent must explain its complete logical placement.
    for path, folder_id in paths.items():
        state = states.get(folder_id) or normalize(metadata_by_id[folder_id])
        if state["id"] != folder_id or state["mime_type"] != FOLDER:
            raise SafetyError("NESTED_DESTINATION_NOT_FOLDER")
        effects = by_source.get(folder_id, {})
        final_name = effects.get("RENAME", {}).get("exact_target_name", state["name"])
        if path == anchor:
            # Canonical sheet paths historically trim the outer anchor title.
            if final_name.strip() != path.rsplit("/", 1)[-1].strip() or effects:
                raise SafetyError("NESTED_ANCHOR_IDENTITY_MISMATCH")
            continue
        parent_path, basename = path.rsplit("/", 1)
        final_parents = effects.get("MOVE", {}).get("target_parent_ids", state["parents"])
        # Spreadsheet paths are logical labels: a folder's explicit rename can
        # differ while descendants still refer to its old logical path. The
        # stable ID, canonical MOVE path (or rename row's old/new title), exact
        # approved title and final parent remain independently bound.
        if "MOVE" in effects:
            move_row = source["records"][effects["MOVE"]["reference"]]["row"]
            if move_row.get("Exact target path") != path:
                raise SafetyError("NESTED_SOURCE_FOLDER_PATH_MISMATCH")
        elif "RENAME" in effects:
            rename_row = source["records"][effects["RENAME"]["reference"]]["row"]
            row_parent = rename_row.get("Proposed destination folder")
            if path not in {str(row_parent) + "/" + state["name"], str(row_parent) + "/" + final_name}:
                raise SafetyError("NESTED_SOURCE_FOLDER_PATH_MISMATCH")
        expected_name = effects.get("RENAME", {}).get("exact_target_name", basename)
        if parent_path not in paths or final_name != expected_name or final_parents != [paths[parent_path]]:
            raise SafetyError("NESTED_DESTINATION_FOLDER_PLACEMENT_MISMATCH")
    return None


def _destination_files_profile(journal, projection, members, operations, boundary, metadata_by_id, source, normalize):
    """Route exact assigned leaves into destination containers; never move origins."""
    from .section_prepare import select_records, resolve_destination_paths
    original = {**source, "records": {ref:r for ref,r in source["records"].items() if not r.get("derived_from_reference")}}
    section_path = projection.get("section_path")
    if not isinstance(section_path,str) or "/" not in section_path:
        raise SafetyError("DESTINATION_SECTION_PATH_REQUIRED")
    anchor=section_path.rsplit("/",1)[0]
    paths=projection.get("destination_paths")
    if not isinstance(paths,dict) or anchor not in paths or section_path not in paths or len(set(paths.values()))!=len(paths):
        raise SafetyError("DESTINATION_PATHS_AMBIGUOUS")
    selected=select_records(original,section_path)
    states={r["source_id"]:normalize(metadata_by_id[r["source_id"]]) for r in selected}
    containers={sid for sid,state in states.items() if state["mime_type"]==FOLDER}
    leaves=set(states)-containers
    if containers!=set(projection.get("source_container_ids",[])) or containers & set(paths.values()):
        raise SafetyError("SOURCE_CONTAINERS_MUST_REMAIN_IN_PLACE")
    if set(members)!=leaves or any(m["ancestor_move_operation_refs"] for m in members.values()):
        raise SafetyError("DESTINATION_LEAF_MEMBERSHIP_MISMATCH")
    if {o["source_id"] for o in operations.values()}!=leaves:
        raise SafetyError("DESTINATION_SOURCE_MUTATION_SCOPE_MISMATCH")
    leaf_records=[r for r in selected if r["source_id"] in leaves]
    existing_moves={r["source_id"] for r in leaf_records if r["action"]=="MOVE"}
    expected_refs={r["reference"] for r in leaf_records}
    expected_refs.update("ROUTE/"+r["reference"] for r in leaf_records if r["action"]=="RENAME" and r["source_id"] not in existing_moves)
    if set(operations)!=expected_refs:
        raise SafetyError("DESTINATION_OPERATION_SET_MISMATCH")
    resolved=resolve_destination_paths(selected,metadata_by_id,{anchor:paths[anchor]},section_path,
                                       method_profile="destination_scoped_files")
    names=resolved["destination_folder_names"]
    if (projection.get("destination_folder_names")!=names
            or projection.get("source_container_refs")!=resolved["source_container_refs"]
            or set(paths)!=set(names)|{anchor}):
        raise SafetyError("DESTINATION_CONTAINER_DEFINITION_MISMATCH")
    proof=journal.read_evidence(projection.get("destination_path_evidence_ref"))
    if (proof.get("schema_version")!="uqmc.section.destination-paths.v1"
            or proof.get("section_id")!=projection["section_id"] or proof.get("input_sha256")!=source["input_sha256"]
            or proof.get("destination_paths")!=paths or not proof.get("provenance_ref")):
        raise SafetyError("DESTINATION_PATH_EVIDENCE_MISMATCH")
    journal.read_evidence(proof["provenance_ref"])
    for path,sid in paths.items():
        state=normalize(metadata_by_id[sid])
        if state["id"]!=sid or state["mime_type"]!=FOLDER:
            raise SafetyError("DESTINATION_NOT_FOLDER")
        if path==anchor:
            if state["name"].strip()!=anchor.rsplit("/",1)[-1].strip():
                raise SafetyError("DESTINATION_ANCHOR_MISMATCH")
        elif state["name"]!=names[path] or state["parents"]!=[paths[path.rsplit("/",1)[0]]]:
            raise SafetyError("DESTINATION_CONTAINER_PLACEMENT_MISMATCH")
    groups={}
    for ref,op in operations.items():
        if op["action"] in groups.setdefault(op["source_id"],{}):
            raise SafetyError("DESTINATION_REPEATED_SOURCE_EFFECT")
        groups[op["source_id"]][op["action"]]=op
    expected_boundary=set()
    for ref,op in operations.items():
        record=source["records"][ref];state=states[op["source_id"]]
        if op["action"]=="MOVE":
            parent_path=(record["row"]["Origin rename row"]["Proposed destination folder"]
                         if record.get("derived_from_reference") else record["row"]["Exact target path"].rsplit("/",1)[0])
            if parent_path not in paths or op["target_parent_ids"]!=[paths[parent_path]]:
                raise SafetyError("DESTINATION_ROUTE_TARGET_MISMATCH")
            expected_boundary.add(paths[parent_path])
            rename=groups[op["source_id"]].get("RENAME")
            if rename and rename["reference"] not in op["depends_on_refs"]:
                raise SafetyError("DESTINATION_MOVE_REQUIRES_RENAME_DEPENDENCY")
        else:
            _workbook_rename_target(journal,record,op,state)
            parent_path=record["row"]["Proposed destination folder"]
            move=groups[op["source_id"]].get("MOVE")
            if parent_path not in paths or not move or move["target_parent_ids"]!=[paths[parent_path]]:
                raise SafetyError("DESTINATION_RENAME_PARENT_MISMATCH")
    if boundary!=expected_boundary:
        raise SafetyError("DESTINATION_BOUNDARY_NOT_EXACT")
    return None



def _hybrid_profile(journal, projection, members, operations, boundary, metadata_by_id, source, normalize):
    """Rebuild the deterministic mixed/intact plan from sealed current evidence.

    A matching row hash is insufficient: exact targets, reuse scope, dependencies
    and logical path provenance must all agree with the independent derivation.
    """
    from .destination_planner import build_hybrid_preparation, logical_parent
    original = {**source, "records": {ref: record for ref, record in source["records"].items()
                                     if not record.get("derived_from_reference")}}
    # Rebuild with the same durable exclusion authority as preparation. A
    # cleared spreadsheet cell never reopens an already excluded identity/ref.
    records = journal.records()
    original["excluded_ids"] = sorted(set(original.get("excluded_ids", [])) | {
        e["payload"]["file_id"] for e in records if e["kind"] == "EXCLUDED"})
    original["excluded_refs"] = sorted(set(original.get("excluded_refs", [])) | {
        e["payload"]["proposal_ref"] for e in records if e["kind"] == "PROPOSAL_EXCLUDED"})
    paths = projection.get("destination_paths")
    section_path = projection.get("section_path")
    if not isinstance(paths, dict) or not isinstance(section_path, str) or not section_path:
        raise SafetyError("HYBRID_DESTINATION_PATHS_REQUIRED")
    proof = journal.read_evidence(projection.get("destination_path_evidence_ref"))
    if (proof.get("schema_version") != "uqmc.section.destination-paths.v1"
            or proof.get("section_id") != projection["section_id"]
            or proof.get("input_sha256") != source["input_sha256"]
            or proof.get("destination_paths") != paths or not proof.get("provenance_ref")):
        raise SafetyError("HYBRID_DESTINATION_PATH_EVIDENCE_MISMATCH")
    journal.read_evidence(proof["provenance_ref"])
    normalized = {sid: normalize(state) for sid, state in metadata_by_id.items()}
    rebuilt = build_hybrid_preparation(original, section_path=section_path,
        section_id=projection["section_id"], workbook_id=projection["workbook_id"],
        metadata_by_id=normalized, destination_paths=paths,
        destination_path_evidence_ref=projection["destination_path_evidence_ref"],
        native_conversion_refs={ref: operation["native_display_conversion_ref"]
            for ref, operation in operations.items() if operation.get("native_display_conversion_ref")},
        extension_corrections={ref: operation["extension_correction"]
            for ref, operation in operations.items() if operation.get("extension_correction")})
    if projection != rebuilt["projection"]:
        raise SafetyError("HYBRID_DETERMINISTIC_PROJECTION_MISMATCH")
    anchor = logical_parent(section_path)
    groups = {}
    for ref, operation in operations.items():
        groups.setdefault(operation["source_id"], {})[operation["action"]] = operation
        if operation["action"] == "RENAME":
            _workbook_rename_target(journal, source["records"][ref], operation,
                                   normalized[operation["source_id"]])
    reused = set(projection["reused_folder_ids"])
    for path, sid in paths.items():
        state = normalized[sid]
        if state["id"] != sid or state["mime_type"] != FOLDER or state.get("trashed"):
            raise SafetyError("HYBRID_DESTINATION_NOT_FOLDER")
        effects = groups.get(sid, {})
        if path == anchor:
            if effects or (path and state["name"].strip() != path.rsplit("/", 1)[-1].strip()):
                raise SafetyError("HYBRID_ANCHOR_IDENTITY_MISMATCH")
            continue
        parent_path = logical_parent(path)
        expected_name = projection["destination_folder_names"][path]
        if sid not in reused and effects:
            raise SafetyError("HYBRID_UNPROVED_SOURCE_DESTINATION_REUSE")
        name = effects.get("RENAME", {}).get("exact_target_name", state["name"])
        parents = effects.get("MOVE", {}).get("target_parent_ids", state["parents"])
        if parent_path not in paths or name != expected_name or parents != [paths[parent_path]]:
            raise SafetyError("HYBRID_DESTINATION_FOLDER_PLACEMENT_MISMATCH")
    return None


def import_section(journal, export, *, projection, owner_approval_ref, expected_section_id,
                   batch_id, metadata_by_id, resolutions, expected_workbook_id=WORKBOOK_ID,
                   expected_method_profile="new_destination_individual_files", review_context_ref=None,
                   metadata_profile="STRICT"):
    """Validate the whole section, then derive exact move refs without row ticks.

    Structural/stale approval errors raise SafetyError. Unresolved selected rows,
    withdrawals or incomplete folder scope return plan=None with a manifest.
    Supplied owner evidence must originate from trusted orchestration; this module
    verifies binding/integrity, not the identity or truth of a human statement.
    """
    with journal.locked():
        normalize = snapshot
        plan_validator = lambda plan: plan.validate()
        if metadata_profile == "AGENT_ASSISTED":
            from .agent_profile import agent_snapshot, validate_agent_plan
            normalize, plan_validator = agent_snapshot, validate_agent_plan
        elif metadata_profile != "STRICT":
            raise SafetyError("UNKNOWN_METADATA_PROFILE")
        source = _destination_catalog(catalog(export, expected_workbook_id=expected_workbook_id), projection)
        if projection.get("method_profile") != expected_method_profile:
            raise SafetyError("SECTION_METHOD_PROFILE_NOT_AUTHORIZED")
        members, operations, boundary = _projection(projection, source, expected_section_id, expected_workbook_id)
        approval = _approval(journal, owner_approval_ref, projection, members, operations, boundary)
        # Persist all current exclusions before any metadata access or early block.
        excluded_sources = {r["payload"]["file_id"] for r in journal.records() if r["kind"] == "EXCLUDED"}
        excluded_refs = {r["payload"]["proposal_ref"] for r in journal.records() if r["kind"] == "PROPOSAL_EXCLUDED"}
        for source_id in source["excluded_ids"]:
            if source_id not in excluded_sources:
                journal.append("EXCLUDED", payload={"file_id": source_id, "reason_code": "WORKBOOK_WITHDRAWN_OR_EXCLUDED"})
        quarantined_refs = {ref for row in source.get("quarantined_tombstones", []) for ref in row["refs"]}
        for ref in source["excluded_refs"]:
            if ref not in excluded_refs:
                reason = "WORKBOOK_REDACTED_EXCLUSION" if ref in quarantined_refs else "WORKBOOK_DECISION_NO"
                journal.append("PROPOSAL_EXCLUDED", payload={"proposal_ref": ref, "reason_code": reason})
        excluded_sources |= set(source["excluded_ids"])
        excluded_refs |= set(source["excluded_refs"])
        target_sections = {r.get("parent_section_id") for r in projection.get("destination_requirements", [])}
        target_sections.add(projection.get("reuse_target", {}).get("target_section_id"))
        if projection["method_profile"] in {"approved_nested_section", "destination_scoped_files", "destination_hybrid"}:
            target_sections.update(projection.get("destination_paths", {}).values())
        support = set(projection.get("supporting_container_ids", []))
        if (set(members) | support) & excluded_sources or (boundary | target_sections) & excluded_sources:
            return _blocked(journal, projection, owner_approval_ref, approval, batch_id, "SECTION_CONTAINS_EXCLUDED_SOURCE_OR_TARGET", review_context_ref)
        for record in source["records"].values():
            if record["action"] == "MOVE" and record["source_id"] in set(members) | support and (record["reference"] in excluded_refs or set(record["proposal_refs"]) & excluded_refs):
                return _blocked(journal, projection, owner_approval_ref, approval, batch_id, "SECTION_CONTAINS_EXCLUDED_MOVE_RECORD", review_context_ref)
        if projection["method_profile"] == "new_destination_individual_files":
            try:
                reason = _individual_profile(journal, projection, members, operations, boundary, metadata_by_id, review_context_ref)
            except KeyError:
                reason = "LIVE_METADATA_MISSING"
            except SafetyError as error:
                reason = str(error)
            if reason:
                return _blocked(journal, projection, owner_approval_ref, approval, batch_id, reason, review_context_ref)
        elif projection["method_profile"] == "existing_folder_consolidation":
            try:
                reason = _reuse_profile(journal, projection, members, operations, boundary, metadata_by_id, review_context_ref, approval, source, normalize)
            except KeyError:
                reason = "LIVE_METADATA_MISSING"
            except SafetyError as error:
                reason = str(error)
            if reason:
                return _blocked(journal, projection, owner_approval_ref, approval, batch_id, reason, review_context_ref)
        elif projection["method_profile"] == "approved_nested_section":
            try:
                reason = _nested_profile(journal, projection, members, operations, boundary, metadata_by_id, source, normalize)
            except KeyError:
                reason = "LIVE_METADATA_MISSING"
            except SafetyError as error:
                reason = str(error)
            if reason:
                return _blocked(journal, projection, owner_approval_ref, approval, batch_id, reason, review_context_ref)
        elif projection["method_profile"] == "destination_scoped_files":
            try:
                reason = _destination_files_profile(journal, projection, members, operations, boundary, metadata_by_id, source, normalize)
            except KeyError:
                reason = "LIVE_METADATA_MISSING"
            except SafetyError as error:
                reason = str(error)
            if reason:
                return _blocked(journal, projection, owner_approval_ref, approval, batch_id, reason, review_context_ref)
        elif projection["method_profile"] == "destination_hybrid":
            try:
                reason = _hybrid_profile(journal, projection, members, operations, boundary, metadata_by_id, source, normalize)
            except KeyError:
                reason = "LIVE_METADATA_MISSING"
            except SafetyError as error:
                reason = str(error)
            if reason:
                return _blocked(journal, projection, owner_approval_ref, approval, batch_id, reason, review_context_ref)
        token = _Eligibility(_SEAL, source["input_sha256"], frozenset(operations), owner_approval_ref,
                             digest(approval), approval["approval_id"],
                             tuple(r for r in source["records"].values() if r.get("derived_from_reference")))
        for old_id in _strings(approval.get("supersedes_approval_ids", []), "SECTION_SUPERSESSION_INVALID", empty=True):
            if old_id == approval["approval_id"]:
                raise SafetyError("SECTION_SELF_SUPERSESSION")
            if not any(r["kind"] == "SECTION_APPROVAL_SUPERSEDED" and r["payload"].get("approval_id") == old_id for r in journal.records()):
                journal.append("SECTION_APPROVAL_SUPERSEDED", payload={"approval_id": old_id, "superseded_by": approval["approval_id"], "owner_approval_ref": owner_approval_ref})
        # Bind destination and dependency resolutions to exactly the frozen section.
        scoped_resolutions = {}
        for ref, operation in operations.items():
            resolved = resolutions.get(ref)
            if resolved is not None:
                target_matches = (set(resolved.get("target_parent_ids", [])) == set(operation["target_parent_ids"])) if operation["action"] == "MOVE" else resolved.get("exact_target_name") == operation["exact_target_name"]
                if not target_matches or resolved.get("depends_on_refs") != operation["depends_on_refs"] or set(resolved.get("allowed_collision_ids", [])) != set(operation.get("allowed_collision_ids", [])) or resolved.get("extension_correction") != operation.get("extension_correction"):
                    raise SafetyError("SECTION_RESOLUTION_SCOPE_MISMATCH")
                scoped_resolutions[ref] = copy.deepcopy(resolved)
        imported = import_workbook(journal, export, batch_id=batch_id, selected_refs=list(operations),
                                   metadata_by_id=metadata_by_id, resolutions=scoped_resolutions,
                                   expected_input_sha256=source["input_sha256"], expected_workbook_id=expected_workbook_id,
                                   _section_eligibility=token, metadata_profile=metadata_profile)
        plan = imported.plan
        scope_reason = None
        exclusions = {r["payload"]["file_id"] for r in journal.records() if r["kind"] == "EXCLUDED"}
        excluded_refs = {r["payload"]["proposal_ref"] for r in journal.records() if r["kind"] == "PROPOSAL_EXCLUDED"}
        if set(members) & exclusions or boundary & exclusions:
            scope_reason = "SECTION_CONTAINS_EXCLUDED_SOURCE_OR_TARGET"
        for record in source["records"].values():
            if record["action"] == "MOVE" and record["source_id"] in members:
                if record["reference"] in excluded_refs or set(record["proposal_refs"]) & excluded_refs:
                    scope_reason = "SECTION_CONTAINS_EXCLUDED_MOVE_RECORD"
        if plan is not None and scope_reason is None:
            # Complete folder snapshots prove the full indirect scope. Never read
            # individual descendants or manufacture MOVE operations for them.
            expected_ancestors = {source_id: set() for source_id in members}
            observed_scope = {op.file_id for op in plan.operations}
            ref_to_op = dict(zip(operations, (None for _ in operations)))
            op_by_id = {op.operation_id: op for op in plan.operations}
            for row in imported.manifest["rows"]:
                ref_to_op[row["reference"]] = op_by_id[row["operation_id"]]
            for ref, op in ref_to_op.items():
                before = normalize(op.before)
                descendants = {m["id"] for m in before["subtree"].get("members", [])} if before["mime_type"] == FOLDER else set()
                observed_scope |= descendants
                if op.action == "MOVE":
                    for source_id in descendants & set(members):
                        expected_ancestors[source_id].add(ref)
            if observed_scope != set(members) | support:
                scope_reason = "SECTION_MEMBERSHIP_NOT_PROVEN_BY_COMPLETE_SCOPE"
            elif any(expected_ancestors[source_id] != set(member["ancestor_move_operation_refs"]) for source_id, member in members.items()):
                scope_reason = "SECTION_INHERITED_MAPPING_MISMATCH"
        if scope_reason:
            plan = None
        binding = {"section_id": expected_section_id, "method_profile": projection["method_profile"], "projection_revision": projection["projection_revision"],
                   "review_context_ref": review_context_ref,
                   "target_mode": projection.get("target_mode"), "supporting_container_ids": sorted(support), "metadata_profile": metadata_profile,
                   "projection_sha256": digest(projection), "owner_approval_ref": owner_approval_ref,
                   "owner_approval_id": approval["approval_id"], "member_source_ids": sorted(members),
                   "operation_refs": list(operations), "allowed_actions": list(projection["allowed_actions"]),
                   "allowed_destination_parent_ids": sorted(boundary), "batch_binding_required": True}
        if plan is not None:
            plan = Plan(plan.run_id, plan.batch_id, "section:" + digest({"scope": binding, "workbook_revision": plan.proposal_revision}),
                        plan.operations, plan.excluded_ids)
            plan_validator(plan)
        manifest = copy.deepcopy(imported.manifest)
        manifest.update(schema_version="uqmc.section.import.v1", authorization_scope=binding,
                        section_approval_validated=True, execution_approved=False,
                        proposal_revision=plan.proposal_revision if plan else None,
                        proposal_hash=plan.proposal_hash if plan else None,
                        plan_ready_for_exact_batch_binding=plan is not None,
                        plan_ready_for_explicit_review=plan is not None,
                        section_reason_code=scope_reason)
        for row in manifest["rows"]:
            if row["state"] == "ELIGIBLE_PENDING_BATCH_APPROVAL":
                row.update(state="ELIGIBLE_PENDING_EXACT_BATCH_BINDING",
                           reason_code="SECTION_APPROVAL_BOUND_EXECUTION_GATES_PENDING",
                           approval_basis="SECTION_OWNER_INSTRUCTION", approval_state="APPROVED")
        if scope_reason:
            for row in manifest["rows"]:
                row.update(state="BLOCKED", reason_code=scope_reason)
        ref_to_id = {row["reference"]: row.get("operation_id") for row in manifest["rows"]}
        manifest["members"] = [{"source_id": member["source_id"],
                                "direct_operation_ids": [ref_to_id.get(ref) for ref in member["direct_operation_refs"]],
                                "ancestor_move_operation_ids": [ref_to_id.get(ref) for ref in member["ancestor_move_operation_refs"]]}
                               for member in projection["members"]]
        evidence_ref = journal.evidence({"projection": projection, "owner_approval_ref": owner_approval_ref,
                                         "workbook_import_ref": imported.evidence_ref, "manifest": manifest,
                                         "plan": plan.body() if plan else None})
        journal.append("SECTION_IMPORTED", payload={"run_id": batch_id, "batch_id": batch_id,
                                                    "projection_sha256": digest(projection), "owner_approval_ref": owner_approval_ref,
                                                    "manifest_ref": evidence_ref, "proposal_hash": manifest["proposal_hash"], "dry_run": True})
        return ImportResult(plan, manifest, evidence_ref)
