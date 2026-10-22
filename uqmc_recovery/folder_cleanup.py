"""Narrow, journaled trailing-whitespace correction for explicitly approved folders.

Call under the existing destination-command lock. No moves, content writes,
permission edits, automatic retries, history rewrites or name-based ID resolution.
"""
from .journal import canonical, digest
from .model import FOLDER, SafetyError


def operation_id(candidate):
    return 'cleanup-' + digest(canonical(candidate))[:24]


def _matches(raw, candidate, name):
    return (raw.get('id') == candidate['id'] and raw.get('name') == name
            and raw.get('mimeType') == FOLDER and raw.get('trashed') is False
            and sorted(raw.get('parents', [])) == sorted(candidate['parents']))


def cleanup_folder(journal, transport, candidate, authority_ref):
    """One authorized folder rename; restart reads before deciding anything."""
    authority = journal.read_evidence(authority_ref)
    if (authority.get('schema_version') != 'uqmc.folder-cleanup.authority.v1'
            or authority.get('decision') != 'APPROVED'
            or candidate not in authority.get('folders', [])):
        raise SafetyError('FOLDER_CLEANUP_AUTHORITY_MISMATCH')
    if (not candidate.get('before_name') or not candidate.get('target_name')
            or candidate['target_name'] != candidate['before_name'].rstrip()
            or candidate['target_name'] == candidate['before_name']
            or len(candidate.get('parents', [])) != 1):
        raise SafetyError('TRAILING_WHITESPACE_CORRECTION_ONLY')
    op = operation_id(candidate)
    history = [e for e in journal.records() if e['operation_id'] == op]
    attempted = any(e['kind'] == 'FOLDER_CLEANUP_ATTEMPTED' for e in history)

    def record(status, raw=None, **extra):
        payload = {'file_id': candidate['id'], 'authority_ref': authority_ref,
                   'candidate': candidate, **extra}
        if raw is not None:
            payload['observed_ref'] = journal.evidence(raw)
        event = journal.append('FOLDER_CLEANUP_' + status, operation_id=op, payload=payload)
        return {'status': status, 'file_id': candidate['id'], 'sequence': event['seq'], **payload}

    before = transport.get_file(candidate['id'])
    if _matches(before, candidate, candidate['target_name']):
        if attempted:
            return record('VERIFIED', before, attribution_reconciled=True)
        return record('BLOCKED', before, reason='TARGET_WITHOUT_RECORDED_ATTEMPT')
    if not _matches(before, candidate, candidate['before_name']):
        return record('BLOCKED', before, reason='UNAPPROVED_CURRENT_STATE')
    if any(e['kind'] == 'FOLDER_CLEANUP_VERIFIED' for e in history):
        return record('BLOCKED', before, reason='CHANGED_AFTER_VERIFIED_CLEANUP')
    if attempted:
        return record('UNCERTAIN', before, reason='PRIOR_DISPATCH_UNRESOLVED_NO_RETRY')
    if before.get('capabilities', {}).get('canRename') is False:
        return record('BLOCKED', before, reason='CANNOT_RENAME')
    siblings = transport.list_children(candidate['parents'][0])
    collisions = [s['id'] for s in siblings if s['id'] != candidate['id']
                  and s.get('name', '').rstrip() == candidate['target_name']]
    if collisions:
        return record('BLOCKED', before, reason='SIBLING_NAME_COLLISION', collision_ids=collisions)
    # Re-read after listing; reject changes during preflight and pass a real
    # HTTP ETag if available. Without it, a small remote race window remains.
    fresh = transport.get_file(candidate['id'])
    if (not _matches(fresh, candidate, candidate['before_name'])
            or any(fresh.get(k) != before.get(k) for k in ('version', 'modifiedTime', 'capabilities'))):
        return record('BLOCKED', fresh, reason='CHANGED_DURING_PREFLIGHT')
    before_ref = journal.evidence(fresh)
    intent = {'fileId': candidate['id'], 'name': candidate['target_name']}
    common = {'file_id': candidate['id'], 'candidate': candidate, 'authority_ref': authority_ref,
              'before_ref': before_ref, 'intent': intent}
    journal.append('FOLDER_CLEANUP_PREPARED', operation_id=op, payload=common)
    journal.append('FOLDER_CLEANUP_ATTEMPTED', operation_id=op, payload=common)
    response_ref = None
    failure = None
    try:
        response_ref = journal.evidence(transport.update_metadata(intent, etag=fresh.get('_http_etag')))
    except Exception as error:
        # Transport sanitizes its errors. Keep only the class/outcome here;
        # never expose arbitrary exception strings or credential material.
        failure = {'error_class': type(error).__name__, 'outcome': getattr(error, 'outcome', 'UNKNOWN')}
    try:
        observed = transport.get_file(candidate['id'])
    except Exception as error:
        return record('UNCERTAIN', reason='READBACK_UNAVAILABLE', before_ref=before_ref,
                      response_ref=response_ref, failure=failure, read_error_class=type(error).__name__)
    if _matches(observed, candidate, candidate['target_name']):
        return record('VERIFIED', observed, before_ref=before_ref, response_ref=response_ref,
                      failure=failure, attribution_reconciled=bool(failure))
    if _matches(observed, candidate, candidate['before_name']):
        return record('UNCERTAIN', observed, before_ref=before_ref, response_ref=response_ref,
                      failure=failure, reason='UNCHANGED_AFTER_DISPATCH_NO_RETRY')
    return record('BLOCKED', observed, before_ref=before_ref, response_ref=response_ref,
                  failure=failure, reason='READBACK_CONFLICT')
