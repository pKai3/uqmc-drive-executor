"""Read-only workbook export -> frozen candidate Plan. Never grants approval.

The workbook is a proposal input, not an executor. This module consumes bounded,
complete connector range exports and separately verified metadata/ID resolutions.
It contains no network calls and never opens a Drive file.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .shortcut_policy import SHORTCUT_MIME, persist_shortcut_exclusions
from .model import FOLDER, NATIVE, MIME_EXTENSIONS, Operation, Plan, SafetyError, WITHDRAWN_ID, digest, snapshot, validate_extension_evidence

# Live identity is supplied from ignored private configuration, never from a repo.
WORKBOOK_ID = "fixture-workbook"
HEADERS = {
    "Proposed Moves": ["Proposal ID", "Current item", "Source path", "Proposed target path", "Action", "Rationale", "Visible descendants / 1 file", "Priority", "Dependencies", "Access concerns", "Decision (Yes/No)", "Status", "Target exists?", "Coverage / exceptions", "Last verified"],
    "Move Manifest": ["Current item", "Exact target path", "Scope", "Visible scope items", "Source path", "Rationale", "Access / dependencies", "Exceptions", "Last verified", "Proposal ID", "Manifest item", "Source ID"],
    "Proposed Renames": ["Rename necessity (guide)", "Current name + type suffix", "Proposed name + type suffix", "Necessity rationale (assistant)", "Decision (Yes/No)", "Review state", "Proposed destination folder", "Current source path", "Evidence / rationale", "Checks before execution", "Document date / year", "Evidence confidence", "Type code", "Rename ID", "Move proposal ID", "Manifest item", "Source ID", "Verified on"],
    "Rename Coverage": ["Rename necessity", "Assessment", "Current name + type suffix", "Proposed name / retained name", "Necessity rationale (assistant)", "Rename proposal", "Source link", "Existing proposed target", "Evidence / rationale", "Remaining checks", "Item kind", "Assessment scope", "Move proposal", "Manifest item / root reference", "Source ID", "Verified MIME type", "Verified on", "Coverage basis"],
}
# Optional in historical frozen captures; new captures include this identity map.
# These rows bind destination context, never grant extra source operations.
OPTIONAL_HEADERS = {"Destination Folder Names": ["Proposal ID", "Current folder name", "Proposed folder name",
    "Destination folder path", "Decision (Yes/No)", "Review state", "Source folder ID",
    "Open existing folder", "Necessity", "Checks / rationale"]}
WITHDRAWN_VALUES = {"excluded", "withdrawn"}
RETIRED_MARKER = "[RETIRED: USER_CONFIRMED_DELETED]"
REDACTED_EXCLUSION_LABELS = {
    "Proposed Moves": "Current item",
    "Move Manifest": "Current item",
    "Proposed Renames": "Current name + type suffix",
}
REDACTED_REFERENCE_FIELDS = {
    "Proposed Moves": ("Proposal ID",),
    "Move Manifest": ("Proposal ID", "Manifest item"),
    "Proposed Renames": ("Rename ID", "Move proposal ID", "Manifest item"),
}
REDACTED_EMPTY_FIELDS = {
    "Proposed Moves": {"Source path", "Proposed target path"},
    "Move Manifest": {"Source ID", "Source path", "Exact target path"},
    "Proposed Renames": {"Source ID", "Current source path", "Proposed destination folder", "Proposed name + type suffix"},
}


def _text(value):
    return value if isinstance(value, str) else ""


def _rows(export, tab):
    matrix = export["sheets"].get(tab)
    if not isinstance(matrix, list) or not matrix or not isinstance(matrix[0], list):
        raise SafetyError("MISSING_WORKBOOK_RANGE:" + tab)
    headers = matrix[0]
    if len(headers) != len(set(headers)) or not set((HEADERS | OPTIONAL_HEADERS)[tab]) <= set(headers):
        raise SafetyError("WORKBOOK_HEADER_MISMATCH:" + tab)
    if any(not isinstance(h, str) for h in headers) or "_row" in headers:
        raise SafetyError("INVALID_WORKBOOK_HEADER")
    result = []
    for number, values in enumerate(matrix[1:], 2):
        if not isinstance(values, list) or len(values) > len(headers):
            raise SafetyError("INVALID_WORKBOOK_ROW")
        if not any(v not in (None, "") for v in values):
            continue
        row = {h: copy.deepcopy(values[i]) if i < len(values) else "" for i, h in enumerate(headers)}
        row["_row"] = number
        result.append(row)
    return result


def workbook_fingerprint(export, *, expected_workbook_id=WORKBOOK_ID):
    """Hash exact selected source ranges, including decisions, excluding fetch timestamp."""
    if not expected_workbook_id or export.get("workbook_id") != expected_workbook_id:
        raise SafetyError("WRONG_WORKBOOK")
    tabs=set(export.get("sheets", {}))
    if not set(HEADERS)<=tabs or tabs-set(HEADERS | OPTIONAL_HEADERS):
        raise SafetyError("WORKBOOK_SCOPE_INCOMPLETE")
    if any(export.get("complete", {}).get(tab) is not True for tab in tabs):
        raise SafetyError("WORKBOOK_RANGE_NOT_COMPLETE")
    if not export.get("exported_at_utc"):
        raise SafetyError("WORKBOOK_CAPTURE_TIME_REQUIRED")
    for tab in tabs:
        _rows(export, tab)
    return digest({"workbook_id": export["workbook_id"], "sheets": export["sheets"], "complete": export["complete"]})


def _index(rows, key):
    result = {}
    for row in rows:
        value = _text(row[key]).strip()
        if not value or value in result:
            raise SafetyError("DUPLICATE_OR_MISSING_PROPOSAL_REFERENCE")
        result[value] = row
    return result


def _withdrawn(row, fields):
    return any(_text(row.get(key)).strip().lower() in WITHDRAWN_VALUES for key in fields)


def retired_source_ids(records):
    """Only explicit confirmed-deletion dispositions retire an identity."""
    return {event["payload"]["file_id"] for event in records
            if event.get("kind") == "RETIRED"
            and event.get("payload", {}).get("reason_code") == "USER_CONFIRMED_DELETED"
            and event["payload"].get("effect") == "NOT_APPLIED"
            and isinstance(event["payload"].get("file_id"), str)
            and event["payload"]["file_id"]}


def persist_retirements(journal, source):
    """Preserve exact marked proposal rows without inventing a source observation.

    The marker is an explicit disposition, not a permission to delete or rename.
    This event survives later removal of the cell marker. Existing exclusions
    remain unchanged and retain any independent privacy meaning.
    """
    with journal.locked():
        recorded = retired_source_ids(journal.records())
        for source_id, record in source.get("retired_records", {}).items():
            if source_id in recorded:
                continue
            payload = {"file_id": source_id, "reason_code": "USER_CONFIRMED_DELETED",
                       "effect": "NOT_APPLIED", "proposal_ref": record["proposal_ref"],
                       "input_sha256": source["input_sha256"],
                       "original_mapping": copy.deepcopy(record["original_mapping"])}
            payload["evidence_ref"] = journal.evidence({"schema_version": "uqmc.proposal.retirement.v1", **payload})
            journal.append("RETIRED", payload=payload)
            recorded.add(source_id)
        return recorded


def _quarantined_refs(row, tab):
    """Return only complete retained references, never an inferred identity."""
    references = REDACTED_REFERENCE_FIELDS[tab]
    if any(not isinstance(row.get(key), str) or not row[key] or row[key].strip() != row[key] for key in references):
        raise SafetyError("AMBIGUOUS_REDACTED_EXCLUSION:" + tab)
    allowed = set(HEADERS[tab]) - REDACTED_EMPTY_FIELDS[tab]
    # This exact review placeholder names no destination. It is allowed only
    # inside an already excluded rename row, never as an executable target.
    if tab == "Proposed Renames" and row.get("Proposed destination folder") == "Pending restricted-destination review":
        allowed.add("Proposed destination folder")
    if any(value not in (None, "") for key, value in row.items() if key not in allowed | {"_row"}):
        raise SafetyError("AMBIGUOUS_REDACTED_EXCLUSION:" + tab)
    if tab == "Move Manifest":
        return [row["Proposal ID"] + "/" + row["Manifest item"]]
    return [row[references[0]]]


def _catalog_rows(export, tab, tombstones, quarantined):
    """Separate exact identity-redacted exclusions without recovering identities.

    Raw rows remain in the full workbook fingerprint and evidence. Complete
    retained proposal references are quarantined as durable exclusions; source
    identities, destinations, incomplete references or unknown nonempty fields
    remain ambiguous and fail closed. Historical review text is never executable.
    """
    active = []
    label = REDACTED_EXCLUSION_LABELS.get(tab)
    for row in _rows(export, tab):
        if label is not None and row.get(label) == "Excluded by user":
            if any(value not in (None, "") for key, value in row.items() if key not in {label, "_row"}):
                quarantined.append({"tab": tab, "row": row["_row"], "refs": _quarantined_refs(row, tab)})
            else:
                tombstones.append({"tab": tab, "row": row["_row"]})
        else:
            active.append(row)
    return active


def subtree_coverage_scope(scope):
    """Recognize supported inventory expansion scopes without inferring wording."""
    scope = _text(scope).strip().lower()
    if scope.startswith('whole subtree'):
        return 'whole_subtree'
    if scope == 'exact source-id subtree partition; exclude separately scoped descendants':
        return 'exact_partition'
    return None


def _partition_root_matches(row, root):
    reference = _text(row.get('Manifest item / root reference')).strip()
    item, proposal = root['Manifest item'], root['Proposal ID']
    if reference == item:
        return True
    # DESC identifies the inventory row, not its enclosing source subtree.
    # Only the supported, exact parent assertion supplies that missing link.
    return (re.fullmatch(r'DESC-[A-Za-z0-9_-]+', reference) is not None
            and row.get('Assessment scope') ==
            f'Descendant of {item} under {proposal}; name assessment only, not an additional move')


def _subtree_coverage_records(records, coverage, section_path=None, *, excluded_ids=()):
    """Bind unlisted Keep-name identities to an explicit source-ID subtree row.

    Coverage is an inventory, not independent approval. A unique enclosing
    manifest row supplies move authority. Partition coverage must name that
    exact parent directly or through its explicit DESC assessment scope.
    Explicit item routes always win; inventory presence alone is not binding.
    """
    explicit_ids = {r['source_id'] for r in records.values()}
    roots = [r for r in records.values() if r['action'] == 'MOVE' and r['source_id']
             and subtree_coverage_scope(r['row'].get('Scope'))]
    additions, unbound = {}, []
    for row in coverage:
        sid = _text(row.get('Source ID')).strip()
        if not sid or sid in explicit_ids or sid in excluded_ids or _text(row.get('Assessment')).strip().lower() != 'keep name':
            continue
        pid = _text(row.get('Move proposal')).strip()
        target = _text(row.get('Existing proposed target'))
        if section_path and not target.startswith(section_path + '/'):
            continue
        enclosing = [r for r in roots
                     if target.startswith(_text(r['row'].get('Exact target path')) + '/')]
        matches = [r for r in enclosing if r['row']['Proposal ID'] == pid
                   and (subtree_coverage_scope(r['row'].get('Scope')) != 'exact_partition'
                        or _partition_root_matches(row, r['row']))]
        if not matches:
            if enclosing:
                unbound.append(f"{sid}:Rename Coverage row {row.get('_row', '?')}:"
                               f"expected one of {','.join(sorted(r['reference'] for r in enclosing))}")
            continue
        longest = max(len(r['row']['Exact target path']) for r in matches)
        matches = [r for r in matches if len(r['row']['Exact target path']) == longest]
        if len(matches) != 1:
            raise SafetyError('AMBIGUOUS_WHOLE_SUBTREE_COVERAGE:' + sid)
        parent = matches[0]
        name = _text(row.get('Current name + type suffix'))
        retained = row.get('Proposed name / retained name')
        # Sheets stores year-only retained folder names as numeric cell values.
        if type(retained) in (int, float):
            retained = format(retained, 'g')
        if (not name or retained != name or row.get('Rename proposal') not in (None, '')):
            raise SafetyError('KEEP_NAME_COVERAGE_CONFLICT:' + sid)
        labels = [name]
        suffix = {**NATIVE, 'application/vnd.google-apps.shortcut': '.shortcut'}.get(row.get('Verified MIME type'))
        if suffix and name.endswith(suffix):
            labels.append(name[:-len(suffix)])
        mime = row.get('Verified MIME type')
        uploaded_labels = {name[:-len(ext)] for ext in MIME_EXTENSIONS.get(mime, ())
                           if name.casefold().endswith(ext.casefold())}
        labels.extend(uploaded_labels)
        matched_labels = {label for label in labels if label and target.endswith('/' + label)}
        parents = {target[:-(len(label) + 1)] for label in matched_labels}
        if len(parents) != 1:
            raise SafetyError('KEEP_NAME_COVERAGE_PATH_CONFLICT:' + sid)
        parent_path = parents.pop()
        from .section_prepare import _path
        _path(parent_path)
        ref = 'SUBTREE/' + parent['reference'] + '/' + sid
        if ref in additions:
            raise SafetyError('DUPLICATE_WHOLE_SUBTREE_COVERAGE:' + sid)
        additions[ref] = {'reference': ref, 'proposal_refs': [parent['reference'], *parent['proposal_refs']],
            'action': 'MOVE', 'source_id': sid, 'decision': parent['decision'],
            'withdrawn': parent['withdrawn'], 'proposal': parent.get('proposal'),
            'whole_subtree_origin_reference': parent['reference'],
            'whole_subtree_origin_source_id': parent['source_id'],
            'coverage_target_parent_path': parent_path,
            **({'coverage_exact_name': next(iter(matched_labels)), 'coverage_mime_type': mime}
               if name not in matched_labels and matched_labels <= uploaded_labels else {}),
            'row': {'Exact target path': target, 'Current item': name, 'Source ID': sid,
                    'Scope': 'Whole-subtree covered descendant', 'Origin coverage row': copy.deepcopy(row)},
            'row_sha256': digest({'derivation': 'WHOLE_SUBTREE_COVERAGE_V1',
                                  'root_row_sha256': parent['row_sha256'], 'coverage_row': row})}
    if unbound:
        raise SafetyError('UNBOUND_SUBTREE_COVERAGE:' + ';'.join(unbound))
    return additions


def catalog(export, *, expected_workbook_id=WORKBOOK_ID, include_subtree_coverage=False, subtree_section_path=None):
    """Find explicit source-ID proposal records without retrieving source metadata."""
    input_hash = workbook_fingerprint(export, expected_workbook_id=expected_workbook_id)
    from .destination_mapping import destination_mappings
    mappings=destination_mappings(_rows(export, "Destination Folder Names")) if "Destination Folder Names" in export['sheets'] else {}
    tombstones, quarantined = [], []
    rows_by_tab = {tab: _catalog_rows(export, tab, tombstones, quarantined) for tab in HEADERS}
    moves = _index(rows_by_tab["Proposed Moves"], "Proposal ID")
    quarantine_refs = {ref for item in quarantined for ref in item["refs"]}
    result, excluded, excluded_refs = {}, {WITHDRAWN_ID}, set(quarantine_refs)
    retired_records = {}
    for row in rows_by_tab["Move Manifest"]:
        pid, item = _text(row["Proposal ID"]).strip(), _text(row["Manifest item"]).strip()
        ref = f"{pid}/{item}"
        if not pid or not item or ref in result:
            raise SafetyError("DUPLICATE_OR_MISSING_MANIFEST_REFERENCE")
        proposal = moves.get(pid)
        source_id = _text(row["Source ID"]).strip()
        decision = _text(proposal["Decision (Yes/No)"]).strip() if proposal else ""
        is_excluded = (proposal and _withdrawn(proposal, ("Status", "Decision (Yes/No)"))) or _withdrawn(row, ("Scope", "Exceptions"))
        if decision == "No":
            excluded_refs.update((ref, pid))
        if is_excluded and source_id:
            excluded.add(source_id)
        result[ref] = {"reference": ref, "proposal_refs": [pid, item], "action": "MOVE", "source_id": source_id,
                       "decision": decision, "withdrawn": bool(is_excluded), "row": row, "proposal": proposal,
                       "row_sha256": digest({"manifest": row, "proposal": proposal})}
    for row in rows_by_tab["Proposed Renames"]:
        ref = _text(row["Rename ID"]).strip()
        if not ref or ref in result:
            raise SafetyError("DUPLICATE_OR_MISSING_RENAME_REFERENCE")
        source_id = _text(row["Source ID"]).strip()
        decision = _text(row["Decision (Yes/No)"]).strip()
        is_excluded = _withdrawn(row, ("Review state", "Decision (Yes/No)"))
        rationale = _text(row.get("Evidence / rationale")).splitlines()
        is_retired = bool(rationale and rationale[0] == RETIRED_MARKER)
        if is_retired:
            if not source_id:
                raise SafetyError("RETIRED_SOURCE_ID_REQUIRED")
            if source_id in retired_records:
                raise SafetyError("DUPLICATE_RETIRED_SOURCE")
            retired_records[source_id] = {"proposal_ref": ref, "original_mapping": {
                "rename_row": copy.deepcopy(row),
                "move_rows": [copy.deepcopy(record["row"]) for record in result.values()
                              if record["action"] == "MOVE" and record["source_id"] == source_id]}}
        if decision == "No":
            excluded_refs.add(ref)
        if is_excluded and source_id:
            excluded.add(source_id)
        result[ref] = {"reference": ref, "proposal_refs": [ref], "action": "RENAME", "source_id": source_id,
                       "decision": decision, "withdrawn": is_excluded, "row": row,
                       "row_sha256": digest(row)}
    for row in rows_by_tab["Rename Coverage"]:
        if _withdrawn(row, ("Assessment", "Rename necessity", "Coverage basis")):
            source_id = _text(row["Source ID"]).strip()
            if source_id:
                excluded.add(source_id)
    shortcut_evidence = {}
    for row in rows_by_tab["Rename Coverage"]:
        sid = _text(row.get("Source ID")).strip()
        if sid and row.get("Verified MIME type") == SHORTCUT_MIME:
            shortcut_evidence.setdefault(sid, []).append(copy.deepcopy(row))
    shortcuts = set(shortcut_evidence)
    coverage_targets = {}
    if include_subtree_coverage:
        result.update(_subtree_coverage_records(result, rows_by_tab["Rename Coverage"], subtree_section_path,
                                               excluded_ids=excluded | set(retired_records) | shortcuts))
        for row in rows_by_tab['Rename Coverage']:
            sid = _text(row.get('Source ID')).strip()
            if sid:
                coverage_targets.setdefault(sid, set()).add(_text(row.get('Existing proposed target')))
    return {**({'subtree_coverage_version': 1,
                'coverage_source_ids': sorted({_text(r.get('Source ID')).strip() for r in rows_by_tab['Rename Coverage'] if _text(r.get('Source ID')).strip()}),
                # Outside-section routes explain retained passengers, but never
                # grant move authority or excuse an unbound in-section child.
                'coverage_target_paths': {sid: sorted(paths) for sid, paths in coverage_targets.items()},
                # Preserve exact same-ID inventory evidence for logical folder aliases.
                # This does not alter proposal rows, row hashes, or move authority.
                'folder_rename_coverage': [copy.deepcopy(r) for r in rows_by_tab['Rename Coverage']
                    if r.get('Verified MIME type') == FOLDER and _text(r.get('Rename proposal')).strip()]} if include_subtree_coverage else {}),
            "destination_folder_mappings": mappings,
            "input_sha256": input_hash, "records": result, "excluded_ids": sorted(excluded | set(retired_records) | shortcuts),
            "shortcut_ids": sorted(shortcuts), "shortcut_evidence": shortcut_evidence,
            "private_excluded_ids": sorted(excluded), "retired_ids": sorted(retired_records), "retired_records": retired_records,
            "excluded_refs": sorted(excluded_refs), "redacted_tombstones": tombstones,
            "quarantined_tombstones": quarantined}


def destination_route_record(rename_record):
    """Derive a typed parent-routing effect from one exact rename destination row.

    This record is usable only through validated section eligibility. It is not
    a spreadsheet edit, inferred per-row approval, or a free-form target.
    """
    if rename_record.get("action") != "RENAME" or not rename_record.get("reference"):
        raise SafetyError("DESTINATION_ROUTE_RENAME_REQUIRED")
    row = rename_record["row"]
    parent, title = row.get("Proposed destination folder"), row.get("Proposed name + type suffix")
    if not isinstance(parent, str) or not parent or not isinstance(title, str) or not title:
        raise SafetyError("DESTINATION_ROUTE_EXACT_ROW_REQUIRED")
    origin = rename_record["reference"]
    return {"reference": "ROUTE/" + origin, "proposal_refs": [origin], "action": "MOVE",
            "source_id": rename_record["source_id"], "decision": rename_record["decision"],
            "withdrawn": rename_record["withdrawn"], "derived_from_reference": origin,
            "row": {"Exact target path": parent + "/" + title, "Scope": "Single item",
                    "Source ID": rename_record["source_id"], "Origin rename row": copy.deepcopy(row)},
            "row_sha256": digest({"derivation": "DESTINATION_ROUTE_FROM_RENAME",
                                  "origin_row_sha256": rename_record["row_sha256"]})}


@dataclass(frozen=True)
class ImportResult:
    plan: Plan | None
    manifest: dict
    evidence_ref: str


def import_workbook(journal, export, *, batch_id, selected_refs, metadata_by_id,
                    resolutions=None, expected_input_sha256=None, expected_workbook_id=WORKBOOK_ID,
                    _section_eligibility=None, metadata_profile="STRICT"):
    """Freeze only an explicitly selected, wholly resolved batch.

    resolutions[reference] binds input_sha256, row_sha256, source_id and supplies
    target_parent_ids (MOVE) or exact_target_name (RENAME), plus depends_on_refs.
    A resolution is review evidence, never approval. Metadata comes from already
    authorized reads. Blocked selected rows produce plan=None, never silent scope
    reduction. Exclusions are persisted before accessing metadata_by_id.
    """
    if not batch_id or not isinstance(batch_id, str):
        raise SafetyError("BATCH_ID_REQUIRED")
    selected = list(selected_refs)
    if len(set(selected)) != len(selected) or not selected:
        raise SafetyError("EXPLICIT_SELECTION_REQUIRED")
    resolutions = resolutions or {}
    normalize = snapshot
    validate_operation = lambda operation: operation.validate()
    validate_plan = lambda plan: plan.validate()
    if metadata_profile == "AGENT_ASSISTED":
        from .agent_profile import agent_snapshot, validate_agent_operation, validate_agent_plan
        normalize, validate_operation, validate_plan = agent_snapshot, validate_agent_operation, validate_agent_plan
    elif metadata_profile != "STRICT":
        raise SafetyError("UNKNOWN_METADATA_PROFILE")
    with journal.locked():
        source = catalog(export, expected_workbook_id=expected_workbook_id)
        input_hash = source["input_sha256"]
        section_eligible_refs = frozenset()
        if _section_eligibility is not None:
            # Internal extension point: only the validated section wrapper can mint
            # this token. It is not a public per-row approval switch.
            from .section_importer import _eligibility_refs, _eligibility_records
            section_eligible_refs = _eligibility_refs(_section_eligibility, input_hash, selected, journal)
            source["records"].update(_eligibility_records(_section_eligibility))
        retired = persist_retirements(journal, source)
        shortcut_ids = persist_shortcut_exclusions(journal, source)
        recorded_exclusions = {r["payload"]["file_id"] for r in journal.records() if r["kind"] == "EXCLUDED"}
        recorded_refs = {r["payload"]["proposal_ref"] for r in journal.records() if r["kind"] == "PROPOSAL_EXCLUDED"}
        for source_id in source["private_excluded_ids"]:
            if source_id not in recorded_exclusions:
                journal.append("EXCLUDED", payload={"file_id": source_id, "reason_code": "WORKBOOK_WITHDRAWN_OR_EXCLUDED", "input_sha256": input_hash})
        quarantine_refs = {value for item in source["quarantined_tombstones"] for value in item["refs"]}
        for ref in source["excluded_refs"]:
            if ref not in recorded_refs:
                reason = "WORKBOOK_REDACTED_EXCLUSION" if ref in quarantine_refs else "WORKBOOK_DECISION_NO"
                journal.append("PROPOSAL_EXCLUDED", payload={"proposal_ref": ref, "reason_code": reason, "input_sha256": input_hash})
        excluded = set(source["excluded_ids"]) | recorded_exclusions | retired | shortcut_ids
        excluded_refs = set(source["excluded_refs"]) | recorded_refs
        rows, candidates = [], {}
        mutation_ids = {source["records"][ref]["source_id"] for ref in selected if ref in source["records"]}
        stale = expected_input_sha256 is not None and expected_input_sha256 != input_hash
        for ref in selected:
            record = source["records"].get(ref)
            entry = {"reference": ref, "state": "BLOCKED", "approval_state": "NOT_APPROVED", "reason_code": None}
            rows.append(entry)
            try:
                if stale:
                    raise SafetyError("WORKBOOK_CHANGED_REVALIDATION_REQUIRED")
                if ref in excluded_refs:
                    entry["approval_state"] = "WITHDRAWN"
                    raise SafetyError("PROPOSAL_EXCLUDED")
                if not record:
                    raise SafetyError("SELECTED_REFERENCE_MISSING")
                source_id = record["source_id"]
                entry.update(source_id=source_id, action=record["action"], row_sha256=record["row_sha256"])
                if not source_id:
                    raise SafetyError("SOURCE_ID_REQUIRED")
                if source_id in excluded:
                    entry["approval_state"] = "WITHDRAWN"
                    raise SafetyError("EXCLUDED")
                if ref in excluded_refs or set(record["proposal_refs"]) & excluded_refs:
                    entry["approval_state"] = "WITHDRAWN"
                    raise SafetyError("PROPOSAL_EXCLUDED")
                if record["decision"] != "Yes" and not (record["decision"] == "" and ref in section_eligible_refs):
                    raise SafetyError("PROPOSAL_DECISION_NOT_YES")
                resolution = resolutions.get(ref)
                if not isinstance(resolution, dict):
                    raise SafetyError("EXACT_TARGET_RESOLUTION_REQUIRED")
                if resolution.get("input_sha256") != input_hash or resolution.get("row_sha256") != record["row_sha256"]:
                    raise SafetyError("STALE_TARGET_RESOLUTION")
                if resolution.get("source_id") != source_id:
                    raise SafetyError("RESOLUTION_SOURCE_MISMATCH")
                dependencies = resolution.get("depends_on_refs")
                if not isinstance(dependencies, list) or any(not isinstance(d, str) or d not in selected or d == ref for d in dependencies):
                    raise SafetyError("EXPLICIT_DEPENDENCY_REVIEW_REQUIRED")
                if resolution.get("reviewed_checks") is not True:
                    raise SafetyError("PROPOSAL_CHECKS_NOT_REVIEWED")
                collisions = resolution.get("allowed_collision_ids", [])
                if not isinstance(collisions, list) or any(not isinstance(item, str) or not item for item in collisions) or len(collisions) != len(set(collisions)):
                    raise SafetyError("INVALID_COLLISION_SCOPE")
                if set(collisions) & excluded:
                    raise SafetyError("EXCLUDED_COLLISION_ID")
                # This is deliberately after all exclusion checks. No metadata access
                # occurs even if an excluded ID exists in the supplied dictionary.
                before = normalize(metadata_by_id[source_id])
                if before["id"] != source_id:
                    raise SafetyError("ID_MISMATCH")
                if resolution.get("live_snapshot_sha256") != digest(before):
                    raise SafetyError("STALE_LIVE_METADATA_RESOLUTION")
                descendant_ids = {x["id"] for x in before.get("subtree", {}).get("members", [])}
                if ({source_id} | set(before["parents"]) | descendant_ids) & excluded:
                    raise SafetyError("EXCLUDED_SCOPE_OR_DESCENDANT")
                if record["action"] == "MOVE":
                    targets = resolution.get("target_parent_ids")
                    if not isinstance(targets, list) or not targets or any(not isinstance(x, str) or not x for x in targets):
                        raise SafetyError("DESTINATION_IDS_REQUIRED")
                    if set(targets) & excluded:
                        raise SafetyError("EXCLUDED_DESTINATION")
                    target = {"parents": sorted(targets)}
                    for parent_id in set(before["parents"]) | set(targets):
                        evidence = metadata_by_id[parent_id]
                        if (metadata_profile == "AGENT_ASSISTED" and parent_id not in mutation_ids
                                and evidence.get("observation_profile") == "READ_ONLY_PARENT_CONTEXT"):
                            from .agent_profile import agent_parent_context
                            parent = agent_parent_context(evidence)
                        else:
                            parent = normalize(evidence)
                        if parent["id"] != parent_id or parent["mime_type"] != FOLDER:
                            raise SafetyError("DESTINATION_OR_ORIGINAL_PARENT_INVALID")
                else:
                    exact_name = resolution.get("exact_target_name")
                    if not isinstance(exact_name, str) or not exact_name:
                        raise SafetyError("EXACT_NATIVE_TITLE_REQUIRED")
                    # Never use the workbook's display title or strip an extension.
                    target = {"name": exact_name}
                operation_id = "op-" + digest({"batch_id": batch_id, "input_sha256": input_hash, "reference": ref, "action": record["action"]})[:24]
                op = Operation(operation_id, source_id, record["action"], before, target,
                               (), tuple(record["proposal_refs"]), allowed_collision_ids=tuple(collisions),
                               extension_correction=copy.deepcopy(resolution.get("extension_correction") or {}))
                validate_operation(op)
                validate_extension_evidence(journal, op)
                candidates[ref] = (op, dependencies)
                entry.update(state="ELIGIBLE_PENDING_BATCH_APPROVAL", reason_code="EXPLICIT_BATCH_APPROVAL_REQUIRED", operation_id=operation_id)
            except KeyError:
                entry["reason_code"] = "LIVE_METADATA_MISSING"
            except SafetyError as error:
                entry["reason_code"] = str(error)
        plan = None
        if len(candidates) == len(selected):
            operations, pending, simulated = [], list(selected), {}
            while pending:
                ready = [ref for ref in pending if set(candidates[ref][1]) <= {r for r, _ in operations}]
                if not ready:
                    for entry in rows:
                        entry.update(state="BLOCKED", reason_code="DEPENDENCY_CYCLE")
                    break
                for ref in ready:
                    op, deps = candidates[ref]
                    before = copy.deepcopy(simulated.get(op.file_id, op.before))
                    # Multiple operations on one item must explicitly order themselves.
                    predecessors = [p for _, p in operations if p.file_id == op.file_id]
                    if predecessors and predecessors[-1].operation_id not in {candidates[d][0].operation_id for d in deps}:
                        for entry in rows:
                            entry.update(state="BLOCKED", reason_code="UNORDERED_SAME_SOURCE")
                        pending = []
                        ready = []
                        break
                    resolved = Operation(op.operation_id, op.file_id, op.action, before, op.target,
                                         tuple(candidates[d][0].operation_id for d in deps), op.proposal_refs,
                                         allowed_collision_ids=op.allowed_collision_ids,
                                         extension_correction=copy.deepcopy(op.extension_correction))
                    operations.append((ref, resolved))
                    simulated[op.file_id] = resolved.intended(before)
                    if resolved.action == "RENAME":
                        # A later ancestor MOVE must include only the already
                        # planned descendant name updates in its exact subtree.
                        updated_ancestors = set()
                        for candidate, _ in candidates.values():
                            if candidate.before["mime_type"] != FOLDER or candidate.file_id == resolved.file_id or candidate.file_id in updated_ancestors:
                                continue
                            updated_ancestors.add(candidate.file_id)
                            ancestor = simulated.setdefault(candidate.file_id, copy.deepcopy(candidate.before))
                            for member in ancestor.get("subtree", {}).get("members", []):
                                if member.get("id") == resolved.file_id and "name" in member:
                                    if member["name"] != before["name"]:
                                        raise SafetyError("PLANNED_DESCENDANT_RENAME_BASELINE_MISMATCH")
                                    member["name"] = resolved.target["name"]
                    pending.remove(ref)
            if len(operations) == len(selected) and all(r["state"] != "BLOCKED" for r in rows):
                proposed = Plan(batch_id, batch_id, "workbook:" + input_hash, tuple(op for _, op in operations), tuple(sorted(excluded)))
                try:
                    validate_plan(proposed)
                    plan = proposed
                except SafetyError as error:
                    for entry in rows:
                        entry.update(state="BLOCKED", reason_code=str(error))
        manifest = {
            "schema_version": "uqmc.workbook.import.v1", "dry_run": True,
            "metadata_profile": metadata_profile,
            "workbook_id": expected_workbook_id, "captured_at_utc": export["exported_at_utc"],
            "input_sha256": input_hash, "proposal_revision": "workbook:" + input_hash,
            "batch_id": batch_id, "selected_refs": selected, "rows": rows,
            "excluded_ids": sorted(excluded), "retired_ids": sorted(retired),
            "excluded_refs": sorted(excluded_refs), "execution_approved": False,
            "redacted_tombstones": source["redacted_tombstones"],
            "quarantined_tombstones": source["quarantined_tombstones"],
            "proposal_hash": plan.proposal_hash if plan else None,
            "plan_ready_for_explicit_review": plan is not None,
        }
        evidence_ref = journal.evidence({"workbook_export": export, "resolutions": resolutions,
                                         "manifest": manifest, "plan": plan.body() if plan else None})
        # IMPORTED is evidence bookkeeping, not an Operation Log state or approval.
        journal.append("IMPORTED", payload={"run_id": batch_id, "batch_id": batch_id,
                                            "input_sha256": input_hash, "manifest_ref": evidence_ref,
                                            "proposal_hash": manifest["proposal_hash"], "dry_run": True})
        return ImportResult(plan, manifest, evidence_ref)


def assert_frozen_input(manifest, current_export):
    """Call on a fresh whole-scope export before separately approving a batch."""
    if workbook_fingerprint(current_export, expected_workbook_id=manifest["workbook_id"]) != manifest["input_sha256"]:
        raise SafetyError("WORKBOOK_CHANGED_REVALIDATION_REQUIRED")
    return True
