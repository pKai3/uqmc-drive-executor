"""Explicit migration scope policy; historical observations remain unchanged."""
from __future__ import annotations

import copy

from .model import FOLDER, SafetyError

SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
REASON = "USER_EXCLUDED_SHORTCUT"


def is_shortcut(metadata):
    return metadata.get("mime_type", metadata.get("mimeType")) == SHORTCUT_MIME


def excluded_shortcut_ids(records):
    return {event["payload"]["file_id"] for event in records
            if event.get("kind") == "PLAN_EXCLUDED"
            and event.get("payload", {}).get("reason_code") == REASON
            and event["payload"].get("mime_type") == SHORTCUT_MIME}


def apply_shortcut_policy(source, metadata_by_id=None, records=()):
    """Catalog metadata and durable scope authority are additive, never privacy flags."""
    ids = set(source.get("shortcut_ids", [])) | excluded_shortcut_ids(records)
    for sid, metadata in (metadata_by_id or {}).items():
        if is_shortcut(metadata):
            ids.add(sid)
            source.setdefault("shortcut_evidence", {}).setdefault(sid, copy.deepcopy(metadata))
    source["shortcut_ids"] = sorted(ids)
    source["excluded_ids"] = sorted(set(source.get("excluded_ids", [])) | ids)
    return ids


def persist_shortcut_exclusions(journal, source):
    """Record exclusion, not deletion, success or resolution of an old request."""
    with journal.locked():
        known = excluded_shortcut_ids(journal.records())
        for sid in sorted(set(source.get("shortcut_ids", [])) - known):
            payload = {"file_id": sid, "mime_type": SHORTCUT_MIME, "reason_code": REASON,
                       "scope_disposition": "EXCLUDED", "historical_effects_preserved": True,
                       "input_sha256": source.get("input_sha256")}
            if source.get("policy_authorization_ref"):
                payload["authorization_ref"] = source["policy_authorization_ref"]
            payload["evidence_ref"] = journal.evidence({
                "schema_version": "uqmc.shortcut-exclusion.v1", **payload,
                "proposal_records": [r for r in source.get("records", {}).values() if r["source_id"] == sid],
                "mime_evidence": source.get("shortcut_evidence", {}).get(sid),
                "policy": "Leave shortcuts in their current location; exclude future operations and active counts."
            })
            journal.append("PLAN_EXCLUDED", payload=payload)
            known.add(sid)
        return known


def guard_shortcut_mutation(before, target):
    if is_shortcut(before):
        raise SafetyError(REASON)
    if (before.get("mime_type", before.get("mimeType")) == FOLDER
            and sorted(before["parents"]) != sorted(target.get("parents", before["parents"]))
            and any(is_shortcut(m) for m in (before.get("subtree") or {}).get("members", []))):
        raise SafetyError("SHORTCUT_DESCENDANT_MUST_STAY_IN_PLACE")


def active_scope_status(source_ids, verified_ids, shortcut_ids):
    """Private policy projection. Does not rewrite any observed execution effect."""
    source, verified, shortcuts = set(source_ids), set(verified_ids), set(shortcut_ids)
    active = source - shortcuts
    return {"schema_version": "uqmc.active-scope.v1", "active_count": len(active),
            "verified_active_count": len(active & verified),
            "remaining_active_ids": sorted(active - verified),
            "excluded_ids": sorted(source & shortcuts), "exclusion_reason": REASON,
            "historical_effects_preserved": True}
