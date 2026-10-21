"""One fresh workbook capture -> immutable pending candidates, never permission.

Paths are labels resolved by separately verified policy. No Drive mutation or
spreadsheet write is exposed. Fresh metadata and exact approval remain required
when an importer binds these candidates to an executable plan.
"""
from __future__ import annotations

import copy

from .importer import catalog
from .model import NATIVE, Operation, SafetyError, digest, validate_rename_name
from .section_importer import validate_section_authority


def _core(value):
    result = {"id": value.get("id"), "name": value.get("name"),
              "parents": value.get("parents"), "mime_type": value.get("mimeType", value.get("mime_type"))}
    if (not all(isinstance(result[k], str) and result[k] for k in ("id", "name", "mime_type")) or
            not isinstance(result["parents"], list) or
            any(not isinstance(v, str) or not v for v in result["parents"]) or
            len(result["parents"]) != len(set(result["parents"]))):
        raise SafetyError("REFRESH_HISTORY_METADATA_INCOMPLETE")
    result["parents"] = sorted(result["parents"])
    return result


def _groups(plan):
    groups = {}
    for operation in plan.ordered():
        if operation.direction != "FORWARD" or operation.action not in {"RENAME", "MOVE"}:
            raise SafetyError("REFRESH_FORWARD_METADATA_PLAN_REQUIRED")
        item = groups.setdefault(operation.file_id, {"original": _core(operation.before),
            "target": _core(operation.before), "operations": {}})
        if operation.action in item["operations"]:
            raise SafetyError("REFRESH_AMBIGUOUS_REPEATED_ACTION")
        item["operations"][operation.action] = operation
        item["target"].update(copy.deepcopy(operation.target))
        item["target"]["parents"] = sorted(item["target"]["parents"])
    return groups


def _validate_policy(journal, plan, policy, binding, groups):
    if not binding or policy.get("section_id") != binding.get("section_id"):
        raise SafetyError("REFRESH_VALID_SECTION_AUTHORITY_REQUIRED")
    ids = policy.get("approved_source_ids")
    if (policy.get("schema_version") != "uqmc.batch-refresh.policy.v1" or
            policy.get("existing_plan_sha256") != plan.proposal_hash or
            not isinstance(policy.get("workbook_id"), str) or not policy["workbook_id"] or
            not isinstance(ids, list) or len(ids) != len(set(ids)) or set(ids) != set(groups)):
        raise SafetyError("REFRESH_POLICY_SCOPE_MISMATCH")
    proof = journal.read_evidence(policy.get("authority_evidence_ref"))
    fields = ("existing_plan_sha256", "section_id", "workbook_id", "approved_source_ids",
              "allow_pending_name_changes", "allow_pending_destination_changes")
    if (proof.get("kind") != "EXPLICIT_PENDING_SECTION_EDIT_AUTHORIZATION" or
            any(proof.get(k) != policy.get(k) for k in fields) or
            any(type(policy.get(k)) is not bool for k in fields[-2:]) or
            not isinstance(proof.get("instruction"), str) or not proof["instruction"].strip() or
            not proof.get("provenance_ref")):
        raise SafetyError("REFRESH_PENDING_EDIT_AUTHORITY_REQUIRED")
    journal.read_evidence(proof["provenance_ref"])
    origin = journal.read_evidence(binding["section_manifest_ref"])
    if origin["manifest"].get("workbook_id") != policy["workbook_id"]:
        raise SafetyError("REFRESH_CANONICAL_WORKBOOK_MISMATCH")
    if binding.get("refresh_snapshot_ref"):
        prior_refresh = journal.read_evidence(binding["refresh_snapshot_ref"])
        previous = journal.read_evidence(prior_refresh["capture_ref"])
    else:
        previous = journal.read_evidence(origin["workbook_import_ref"])["workbook_export"]
    previous_catalog = catalog(previous, expected_workbook_id=policy["workbook_id"])
    destination_paths = policy.get("destination_paths")
    if not isinstance(destination_paths, dict) or not destination_paths:
        raise SafetyError("REFRESH_VERIFIED_DESTINATION_MAPPING_REQUIRED")
    # MOVE cells name an item's full target path; RENAME cells name its
    # containing folder. The same label can therefore resolve to different
    # parents. Only an explicit second map may distinguish those semantics.
    move_paths = policy.get("move_destination_paths", destination_paths)
    if not isinstance(move_paths, dict) or not move_paths:
        raise SafetyError("REFRESH_VERIFIED_MOVE_DESTINATION_MAPPING_REQUIRED")
    boundary = set(origin["projection"]["allowed_destination_parent_ids"])
    for paths in (destination_paths, move_paths):
        for path, mapping in paths.items():
            if (not isinstance(path, str) or not path or not isinstance(mapping, dict) or
                    not isinstance(mapping.get("parent_ids"), list) or not mapping["parent_ids"] or
                    len(mapping["parent_ids"]) != len(set(mapping["parent_ids"])) or
                    not set(mapping["parent_ids"]) <= boundary or not mapping.get("provenance_ref")):
                raise SafetyError("REFRESH_DESTINATION_OUTSIDE_APPROVED_BOUNDARY")
            evidence = journal.read_evidence(mapping["provenance_ref"])
            if (evidence.get("kind") != "VERIFIED_DESTINATION_PATH_BINDING" or
                    evidence.get("section_id") != policy["section_id"] or evidence.get("path") != path or
                    evidence.get("parent_ids") != mapping["parent_ids"]):
                raise SafetyError("REFRESH_DESTINATION_PROVENANCE_MISMATCH")
    return previous_catalog


def _history(journal, records, groups):
    """Observed outcomes across frozen plans are keyed by stable ID, not row."""
    known, pending = {}, {}
    for event in records:
        payload, kind = event["payload"], event["kind"]
        source = payload.get("source_id", payload.get("file_id"))
        if source not in groups:
            continue
        # Bookkeeping with no plan binding must never assert a file outcome.
        if not (payload.get("plan_sha256") or payload.get("proposal_hash")):
            continue
        if kind in {"DESIRED_ATTEMPTED", "ATTEMPTED", "UNCERTAIN"}:
            pending[source] = event["seq"]
        verified = kind in {"DESIRED_APPLIED", "DESIRED_ROLLED_BACK", "VERIFIED_APPLIED", "RECONCILED_AT_TARGET"}
        observed = kind == "DESIRED_OBSERVED" and payload.get("state") in {"TARGET", "PARTIAL", "NOT_APPLIED"}
        if not (verified or observed):
            continue
        ref = payload.get("observed_ref") or payload.get("applied_ref")
        if not ref:
            raise SafetyError("REFRESH_VERIFIED_HISTORY_EVIDENCE_MISSING")
        value = _core(journal.read_evidence(ref))
        if value["id"] != source:
            raise SafetyError("REFRESH_HISTORY_ID_MISMATCH")
        known[source] = {"state": value, "ref": ref, "sequence": event["seq"]}
    unresolved = {source for source, seq in pending.items() if seq > known.get(source, {}).get("sequence", 0)}
    return known, unresolved


def _index_by_source(source):
    result = {}
    for record in source["records"].values():
        result.setdefault(record["source_id"], {}).setdefault(record["action"], []).append(record)
    return result


def _name(journal, policy, record, old_record, group, before):
    proposed = record["row"].get("Proposed name + type suffix")
    if not isinstance(proposed, str) or not proposed:
        raise SafetyError("REFRESH_EXACT_PROPOSED_NAME_REQUIRED")
    old_name = old_record["row"].get("Proposed name + type suffix") if old_record else None
    if proposed == old_name:
        return group["target"]["name"]
    if before["mime_type"] in NATIVE and any(proposed.lower().endswith(s) for s in NATIVE.values()):
        ref = policy.get("native_display_conversion_refs", {}).get(before["id"])
        if not ref:
            raise SafetyError("REFRESH_NATIVE_DISPLAY_CONVERSION_REQUIRED")
        proof = journal.read_evidence(ref)
        suffix = NATIVE[before["mime_type"]]
        target = proof.get("exact_target_name")
        if (proof.get("schema_version") != "uqmc.rename.native-display-conversion.v1" or
                proof.get("source_id") != before["id"] or proof.get("mime_type") != before["mime_type"] or
                proof.get("workbook_row_sha256") != record["row_sha256"] or
                proof.get("workbook_proposed_name") != proposed or proof.get("display_suffix") != suffix or
                not isinstance(target, str) or not target or proposed != target + suffix or not proof.get("provenance_ref")):
            raise SafetyError("REFRESH_NATIVE_DISPLAY_CONVERSION_MISMATCH")
        journal.read_evidence(proof["provenance_ref"])
        return target
    return proposed


def refresh_batch(journal, existing_plan, capture_callable, policy, *, batch_id):
    """Fetch once, reconcile exact section IDs, freeze candidates without approval.

    The caller must supply a real authenticated capture callable, not a cached
    export loader. No stale-input fallback exists. A candidate is not executable:
    the existing importer still needs fresh source snapshots and exact authority.
    """
    if not isinstance(batch_id, str) or not batch_id:
        raise SafetyError("REFRESH_BATCH_ID_REQUIRED")
    groups = _groups(existing_plan)
    with journal.locked():
        binding = validate_section_authority(journal, existing_plan)
        prior = _validate_policy(journal, existing_plan, policy, binding, groups)
        if any(r["kind"] == "BATCH_REFRESHED" and r["payload"].get("batch_id") == batch_id for r in journal.records()):
            raise SafetyError("REFRESH_BATCH_ID_ALREADY_FROZEN")
    # No lock held during the one network capture. Recheck authority afterwards.
    captured = capture_callable()
    fresh = catalog(captured, expected_workbook_id=policy["workbook_id"])
    if not captured.get("source_revision") or not captured.get("capture_sha256"):
        raise SafetyError("REFRESH_CAPTURE_REVISION_AND_HASH_REQUIRED")
    with journal.locked():
        current_binding = validate_section_authority(journal, existing_plan)
        if current_binding != binding:
            raise SafetyError("REFRESH_SECTION_AUTHORITY_CHANGED_DURING_CAPTURE")
        prior = _validate_policy(journal, existing_plan, policy, current_binding, groups)
        records = journal.records()
        recorded_ids = {r["payload"]["file_id"] for r in records if r["kind"] == "EXCLUDED"}
        recorded_refs = {r["payload"]["proposal_ref"] for r in records if r["kind"] == "PROPOSAL_EXCLUDED"}
        relevant_refs = {ref for c in (fresh, prior) for record in c["records"].values()
                         if record["source_id"] in groups
                         for ref in [record["reference"], *record["proposal_refs"]]}
        for source in sorted(set(fresh["excluded_ids"]) & set(groups) - recorded_ids):
            journal.append("EXCLUDED", payload={"file_id": source, "reason_code": "FRESH_WORKBOOK_WITHDRAWN",
                "input_sha256": fresh["input_sha256"]})
        for ref in sorted(set(fresh["excluded_refs"]) & relevant_refs - recorded_refs):
            journal.append("PROPOSAL_EXCLUDED", payload={"proposal_ref": ref, "reason_code": "FRESH_WORKBOOK_NO_OR_WITHDRAWN",
                "input_sha256": fresh["input_sha256"]})
        records = journal.records()
        if any(r["kind"] == "BATCH_REFRESHED" and r["payload"].get("batch_id") == batch_id for r in records):
            raise SafetyError("REFRESH_BATCH_ID_ALREADY_FROZEN")
        known, unresolved = _history(journal, records, groups)
        latest, previous = _index_by_source(fresh), _index_by_source(prior)
        exclusions = set(existing_plan.excluded_ids) | set(fresh["excluded_ids"]) | {r["payload"]["file_id"] for r in records if r["kind"] == "EXCLUDED"}
        excluded_refs = set(fresh["excluded_refs"]) | {r["payload"]["proposal_ref"] for r in records if r["kind"] == "PROPOSAL_EXCLUDED"}
        pending, complete, changed_complete, excluded, blocked = [], [], [], [], []
        for source, group in groups.items():
            try:
                if source in exclusions:
                    excluded.append({"source_id": source, "reason": "WITHDRAWN_OR_EXCLUDED"})
                    continue
                if source in unresolved:
                    raise SafetyError("REFRESH_UNRESOLVED_REQUEST_REQUIRES_RECONCILIATION")
                observed = known.get(source)
                before = copy.deepcopy(observed["state"] if observed else group["original"])
                if before["mime_type"] != group["original"]["mime_type"]:
                    raise SafetyError("REFRESH_HISTORY_MIME_DRIFT")
                if any(before[field] not in (group["original"][field], group["target"][field]) for field in ("name", "parents")):
                    raise SafetyError("REFRESH_HISTORY_CONTAINS_UNAPPROVED_DRIFT")
                target = copy.deepcopy(group["target"])
                refs = []
                rename_destination = None
                source_records = latest.get(source, {})
                if set(source_records) - set(group["operations"]):
                    raise SafetyError("REFRESH_NEW_ACTION_REQUIRES_SCOPE_REVIEW")
                for action, operation in group["operations"].items():
                    matches = source_records.get(action, [])
                    old_matches = previous.get(source, {}).get(action, [])
                    if len(matches) != 1 or len(old_matches) != 1:
                        raise SafetyError("REFRESH_SOURCE_MAPPING_MISSING_OR_AMBIGUOUS")
                    record, old_record = matches[0], old_matches[0]
                    if record["withdrawn"] or record["decision"] == "No" or set(record["proposal_refs"]) & excluded_refs or record["reference"] in excluded_refs:
                        raise SafetyError("REFRESH_APPROVAL_WITHDRAWN")
                    if record["decision"] not in {"Yes", ""} or (old_record["decision"] == "Yes" and record["decision"] != "Yes"):
                        raise SafetyError("REFRESH_APPROVAL_REMOVED_OR_INVALID")
                    refs.append(record["reference"])
                    if action == "RENAME":
                        target["name"] = _name(journal, policy, record, old_record, group, before)
                        rename_destination = record["row"].get("Proposed destination folder")
                    else:
                        path = record["row"].get("Exact target path")
                        mapping = policy.get("move_destination_paths", policy["destination_paths"]).get(path)
                        if not mapping:
                            raise SafetyError("REFRESH_DESTINATION_UNRESOLVED_OR_OUTSIDE_SECTION")
                        target["parents"] = sorted(mapping["parent_ids"])
                if rename_destination:
                    mapping = policy["destination_paths"].get(rename_destination)
                    if not mapping:
                        raise SafetyError("REFRESH_RENAME_DESTINATION_UNRESOLVED_OR_OUTSIDE_SECTION")
                    if sorted(mapping["parent_ids"]) != target["parents"]:
                        raise SafetyError("REFRESH_DESTINATION_COLUMNS_DISAGREE")
                if observed and (before == group["target"] or ("MOVE" in group["operations"] and before["parents"] == group["target"]["parents"] and target != group["target"])):
                    entry = {"source_id": source, "observed": before, "requested_target": target,
                             "history_evidence_ref": observed["ref"] if observed else None, "proposal_refs": refs}
                    (complete if target == group["target"] else changed_complete).append(entry)
                    continue
                if target["name"] != group["target"]["name"] and not policy["allow_pending_name_changes"]:
                    raise SafetyError("REFRESH_PENDING_NAME_EDIT_NOT_AUTHORIZED")
                if target["parents"] != group["target"]["parents"] and not policy["allow_pending_destination_changes"]:
                    raise SafetyError("REFRESH_PENDING_DESTINATION_EDIT_NOT_AUTHORIZED")
                actions = []
                if before["name"] != target["name"]:
                    old_operation = group["operations"].get("RENAME")
                    correction = old_operation.extension_correction if old_operation and target["name"] == old_operation.target["name"] and before["name"] == old_operation.before["name"] else None
                    validate_rename_name(Operation("refresh-candidate", source, "RENAME", before, {"name": target["name"]}, extension_correction=correction), before)
                    actions.append("RENAME")
                if before["parents"] != target["parents"]:
                    actions.append("MOVE")
                pending.append({"source_id": source, "before": before, "target": target, "actions": actions,
                    "proposal_refs": refs, "history_evidence_ref": observed["ref"] if observed else None,
                    "requires_fresh_metadata": True})
            except SafetyError as exc:
                blocked.append({"source_id": source, "reason": str(exc)})
        # New IDs appearing in a known destination are changes in scope, not consent.
        for source, actions in latest.items():
            if source and source not in groups and any(r["row"].get("Exact target path") in policy.get("move_destination_paths", policy["destination_paths"]) for r in actions.get("MOVE", [])):
                blocked.append({"source_id": source, "reason": "REFRESH_NEW_SOURCE_OUTSIDE_APPROVED_SECTION"})
        capture_ref, policy_ref = journal.evidence(captured), journal.evidence(policy)
        snapshot = {"schema_version": "uqmc.batch-refresh.candidates.v1", "batch_id": batch_id,
            "existing_plan_sha256": existing_plan.proposal_hash, "workbook_id": policy["workbook_id"],
            "section_id": policy["section_id"], "captured_at_utc": captured["exported_at_utc"],
            "source_revision": copy.deepcopy(captured["source_revision"]), "input_sha256": fresh["input_sha256"],
            "capture_sha256": captured["capture_sha256"], "capture_ref": capture_ref, "policy_ref": policy_ref,
            "history_sequence": records[-1]["seq"] if records else 0,
            "history_tip_sha256": records[-1]["hash"] if records else None,
            "pending_candidates": pending, "completed_changes_separate_work": changed_complete,
            "completed_unchanged": complete, "excluded": excluded, "blocked": blocked,
            "execution_approved": False}
        ref = journal.evidence(snapshot)
        event = journal.append("BATCH_REFRESHED", payload={"batch_id": batch_id, "existing_plan_sha256": existing_plan.proposal_hash,
            "section_id": policy["section_id"], "snapshot_ref": ref, "snapshot_sha256": digest(snapshot),
            "capture_ref": capture_ref, "input_sha256": fresh["input_sha256"], "execution_approved": False})
        return {"snapshot": snapshot, "evidence_ref": ref, "event_sequence": event["seq"]}
