"""Read-only provenance for destinations created during earlier preparation.

This establishes only the recorded creation identity. The caller must still
match the logical key to the current approved path and verify live metadata.
"""
from __future__ import annotations

from .model import FOLDER, SafetyError, digest

_REVOKED = {'REVOKED', 'SECTION_APPROVAL_REVOKED', 'SECTION_APPROVAL_SUPERSEDED'}


def _fail(reason):
    raise SafetyError('RECORDED_DESTINATION_' + reason)


def _event_ref(event):
    return {'sequence': event['seq'], 'event_id': event['event_id'], 'record_sha256': event['hash']}


def load_created_folder_proofs(journal, section_id):
    """Return exact file-ID proofs for verified allocations in one section.

    No reads of Drive, journal writes or inferred names/paths. Merely allocated
    or uncertain-only creations are omitted. Conflicting or invalid purported
    verification fails closed instead of granting coverage to its identity.
    """
    if not isinstance(section_id, str) or not section_id:
        _fail('SECTION_REQUIRED')
    records = journal.records()
    allocations, verifications, latest_requests = {}, {}, {}
    revoked = {e['payload'].get('approval_id') for e in records if e['kind'] in _REVOKED}
    for event in records:
        value = event['payload']
        if value.get('section_id') != section_id:
            continue
        operation_id = event.get('operation_id')
        if event['kind'] == 'REQUIRED_FOLDER_ALLOCATED':
            if operation_id in allocations:
                _fail('DUPLICATE_ALLOCATION')
            allocations[operation_id] = event
        elif event['kind'] == 'REQUIRED_FOLDER_VERIFIED':
            verifications.setdefault(operation_id, []).append(event)
        if event['kind'] in {'REQUIRED_FOLDER_ATTEMPTED', 'REQUIRED_FOLDER_RECEIPT',
                             'REQUIRED_FOLDER_UNCERTAIN', 'REQUIRED_FOLDER_RECONCILIATION_FAILED'}:
            latest_requests[operation_id] = event['seq']
    candidates, key_to_id = {}, {}
    for operation_id, verified in verifications.items():
        allocated = allocations.get(operation_id)
        if allocated is None:
            _fail('VERIFICATION_WITHOUT_ALLOCATION')
        if latest_requests.get(operation_id, 0) > verified[-1]['seq']:
            _fail('UNRESOLVED_CREATION')
        allocation = allocated['payload']
        required = ('file_id', 'key', 'name', 'parent_id', 'stable_key', 'authority_ref')
        if any(not isinstance(allocation.get(k), str) or not allocation[k] for k in required):
            _fail('ALLOCATION_INVALID')
        stable = digest({'section_id': section_id, 'key': allocation['key']})
        if allocation['stable_key'] != stable or operation_id != 'required-folder-' + stable[:24]:
            _fail('STABLE_KEY_MISMATCH')
        file_id, key = allocation['file_id'], allocation['key']
        if file_id in candidates or key in key_to_id:
            _fail('IDENTITY_CONFLICT')
        for event in verified:
            value = event['payload']
            if event['seq'] <= allocated['seq'] or any(value.get(k) != allocation[k]
                    for k in ('file_id', 'key', 'name', 'parent_id')):
                _fail('VERIFICATION_SCOPE_MISMATCH')
            observed = journal.read_evidence(value.get('observed_ref'))
            if (observed.get('id') != file_id or observed.get('name') != allocation['name']
                    or observed.get('mimeType') != FOLDER
                    or observed.get('parents') != [allocation['parent_id']]
                    or observed.get('trashed') is not False
                    or observed.get('appProperties', {}).get('uqmcFolderKey') != stable):
                _fail('OBSERVATION_MISMATCH')
        candidates[file_id] = (allocated, verified)
        key_to_id[key] = file_id

    authorities = {}
    validating, validated = set(), set()
    def authority_for(reference):
        if reference not in authorities:
            authority = journal.read_evidence(reference)
            if (authority.get('schema_version') != 'uqmc.folder-create-authority.v1'
                    or authority.get('decision') != 'APPROVED'
                    or authority.get('section_id') != section_id
                    or not authority.get('approval_id')
                    or authority.get('access_policy') != 'SCOPED_INHERITANCE_ACCEPTED'):
                _fail('AUTHORITY_SCOPE_MISMATCH')
            if authority['approval_id'] in revoked:
                _fail('AUTHORITY_REVOKED')
            provenance = journal.read_evidence(authority.get('provenance_ref'))
            if provenance.get('kind') != 'TRUSTED_USER_INSTRUCTION' or not provenance.get('text'):
                _fail('USER_INSTRUCTION_REQUIRED')
            folders = authority.get('folders')
            if (not isinstance(folders, list) or any(not isinstance(x, dict) for x in folders)
                    or len({x.get('key') for x in folders}) != len(folders)):
                _fail('AUTHORITY_FOLDERS_INVALID')
            authorities[reference] = authority
        return authorities[reference]

    def validate(file_id):
        if file_id in validated:
            return
        if file_id in validating:
            _fail('PARENT_CYCLE')
        validating.add(file_id)
        allocated, verified = candidates[file_id]
        allocation = allocated['payload']
        # Validate original authority and every subsequent reconciliation, not
        # merely the last convenient event. A new authority cannot erase history.
        for event in [allocated, *verified]:
            value = event['payload']
            authority = authority_for(value.get('authority_ref'))
            matches = [x for x in authority['folders'] if x.get('key') == allocation['key']
                       and x.get('name') == allocation['name']]
            if len(matches) != 1:
                _fail('OUTSIDE_CREATION_AUTHORITY')
            spec = matches[0]
            if 'parent_key' in spec:
                if 'parent_id' in spec or spec['parent_key'] not in key_to_id:
                    _fail('PARENT_DEPENDENCY_UNVERIFIED')
                parent_id = key_to_id[spec['parent_key']]
                if parent_id != allocation['parent_id']:
                    _fail('PARENT_SCOPE_MISMATCH')
                validate(parent_id)
                if not any(e['seq'] < event['seq'] for e in candidates[parent_id][1]):
                    _fail('PARENT_DEPENDENCY_UNVERIFIED')
            elif spec.get('parent_id') != allocation['parent_id']:
                _fail('PARENT_SCOPE_MISMATCH')
        validating.remove(file_id)
        validated.add(file_id)

    result = {}
    for file_id, (allocated, verified) in candidates.items():
        validate(file_id)
        allocation, latest = allocated['payload'], verified[-1]
        result[file_id] = {'file_id': file_id, 'section_id': section_id,
            'logical_key': allocation['key'], 'name': allocation['name'],
            'parent_id': allocation['parent_id'], 'stable_key': allocation['stable_key'],
            'authority_ref': latest['payload']['authority_ref'],
            'allocation_authority_ref': allocation['authority_ref'],
            'observed_ref': latest['payload']['observed_ref'],
            'allocation_event_ref': _event_ref(allocated), 'verification_event_ref': _event_ref(latest)}
    # A successfully sealed earlier batch can retain destinations that a later
    # proposal no longer needs. Bind those paths to journal evidence, not a
    # mutable registry; callers may leave them untouched but never repurpose them.
    for event in records:
        payload = event['payload']
        if (event['kind'] != 'SECTION_IMPORTED' or
                not str(payload.get('run_id', '')).startswith(section_id + '-')):
            continue
        origin = journal.read_evidence(payload['manifest_ref'])
        projection, plan = origin.get('projection', {}), origin.get('plan')
        if plan is None:
            continue
        if (projection.get('section_id') != section_id
                or digest(projection) != payload.get('projection_sha256')
                or digest(plan) != payload.get('proposal_hash')
                or origin.get('manifest', {}).get('section_approval_validated') is not True):
            _fail('REGISTERED_PATH_PROOF_MISMATCH')
        section_path = projection.get('section_path')
        if not isinstance(section_path, str):
            continue
        for path, sid in projection.get('destination_paths', {}).items():
            if sid in result and result[sid]['logical_key'] == 'folder:' + digest({
                    'section_path': section_path, 'logical_path': path}):
                result[sid]['registered_logical_path'] = path
    return result


def load_created_folder_proofs_for_path(journal, section_path):
    """Recover exact/ancestor-created identities inside one requested subtree.

    Ancestor creation authority proves identity only. It is never approval to
    execute that ancestor or to expand the new batch. Callers still validate
    current required paths, live state and the new section's own authority.
    """
    from .section_prepare import _path, _inside
    section_path = _path(section_path)
    section_id = 'destination-' + digest({'canonical_path': section_path})[:20]
    result = {sid: {**proof, 'creation_section_path': section_path}
              for sid, proof in load_created_folder_proofs(journal, section_id).items()}
    records = journal.records()
    path_owners = {}
    parts = section_path.split('/')
    for count in range(1, len(parts)):
        ancestor = '/'.join(parts[:count])
        ancestor_id = 'destination-' + digest({'canonical_path': ancestor})[:20]
        proofs = load_created_folder_proofs(journal, ancestor_id)
        if not proofs:
            continue
        events = {}
        for event in records:
            payload = event['payload']
            if (event['kind'] == 'DESTINATION_VERIFIED'
                    and payload.get('section_id') == ancestor_id
                    and payload.get('file_id') in proofs):
                events.setdefault(payload['file_id'], []).append(event)
        for sid, proof in proofs.items():
            matched = events.get(sid, [])
            requested = [event for event in matched
                         if isinstance(event['payload'].get('proposal_path'), str)
                         and _inside(event['payload']['proposal_path'], section_path)]
            if not requested:
                continue  # Siblings and unbound creation identities stay outside.
            paths = {event['payload'].get('proposal_path') for event in matched}
            if len(paths) != 1:
                _fail('LOGICAL_PATH_CONFLICT')
            logical_path = _path(requested[-1]['payload']['proposal_path'])
            expected_key = 'folder:' + digest({'section_path': ancestor, 'logical_path': logical_path})
            if proof['logical_key'] != expected_key:
                _fail('LOGICAL_KEY_MISMATCH')
            for event in matched:
                payload = event['payload']
                if (event['seq'] <= proof['allocation_event_ref']['sequence']
                        or payload.get('exact_name') != proof['name']
                        or payload.get('parent_ids') != [proof['parent_id']]):
                    _fail('DESTINATION_PATH_EVIDENCE_MISMATCH')
            if sid in result or (logical_path in path_owners and path_owners[logical_path] != sid):
                _fail('IDENTITY_CONFLICT')
            # An exact-section allocation may not yet have its later path event.
            # Compare its stable logical key, rather than inferring from a name.
            exact_key = 'folder:' + digest({'section_path': section_path, 'logical_path': logical_path})
            if any(p['creation_section_path'] == section_path and p['logical_key'] == exact_key
                   for p in result.values()):
                _fail('IDENTITY_CONFLICT')
            result[sid] = {**proof, 'creation_section_path': ancestor, 'logical_path': logical_path,
                           'destination_event_ref': _event_ref(requested[-1])}
            path_owners[logical_path] = sid
    return result


def verified_destination_anchors(journal, section_path, metadata, source_ids):
    """Reuse observed destination identities from this section or its ancestors.

    This supplies structural evidence only. The planner still restricts required
    paths and verifies recovered children and final parent identities.
    """
    from .section_prepare import _path, _inside
    section_path = _path(section_path)
    parts = section_path.split('/')
    section_ids = {'destination-' + digest({'canonical_path': '/'.join(parts[:n])})[:20]
                   for n in range(1, len(parts) + 1)}
    result = {}
    for event in journal.records():
        value = event['payload']; sid = value.get('file_id'); path = value.get('proposal_path')
        if (event['kind'] != 'DESTINATION_VERIFIED' or value.get('section_id') not in section_ids
                or sid in source_ids or sid not in metadata or not isinstance(path, str)
                or not _inside(path, section_path) or not value.get('observed_ref')):
            continue
        observed = journal.read_evidence(value['observed_ref'])
        if (observed.get('id') != sid or observed.get('name') != value.get('exact_name')
                or observed.get('parents') != value.get('parent_ids')
                or observed.get('mime_type', observed.get('mimeType')) != FOLDER
                or observed.get('trashed') is not False):
            raise SafetyError('RECOVERY_DESTINATION_ANCHOR_EVIDENCE_CONFLICT:' + sid)
        current = metadata[sid]
        marker = (observed.get('appProperties') or {}).get('uqmcFolderKey')
        if (current.get('id') != sid or current.get('mime_type') != FOLDER
                or current.get('trashed') is not False or current.get('name') != value['exact_name']
                or current.get('parents') != value['parent_ids']
                or (marker is not None and (current.get('appProperties') or {}).get('uqmcFolderKey') != marker)):
            continue  # Mismatched evidence cannot satisfy the planner's parent guard.
        if path in result and result[path] != sid:
            raise SafetyError('RECOVERY_DESTINATION_ANCHOR_AMBIGUOUS:' + path)
        result[path] = sid
    return result
