"""Pure, deterministic hybrid destination planning; no I/O or execution authority.

A folder's display-node ID is never sufficient to reuse it. Reuse requires a
complete live subtree whose individually assigned routes preserve its structure.
Other source containers stay in place while eligible descendants are extracted.
"""
from __future__ import annotations

import copy
from .agent_profile import agent_snapshot
from .importer import destination_route_record
from .model import FOLDER, SafetyError, digest
from .section_prepare import (_inside, _path, _record_path as nested_record_path, _order_groups,
                              exact_rename, select_records as nested_select_records)

PROFILE = 'destination_hybrid'


def logical_parent(path):
    """The empty logical alias denotes only an explicitly configured project root."""
    _path(path)
    return path.rsplit('/', 1)[0] if '/' in path else ''


def _record_path(record):
    if record['action'] == 'RENAME' and record['row'].get('Proposed destination folder') == '':
        return ''
    return nested_record_path(record)


def file_parent_path(move, rename, state):
    """Resolve a file route without mistaking literal title slashes for folders.

    Exact manifest/current-name labels describe the suffix used by the sheet.
    A matching complete suffix wins over parsing its internal slashes. Without
    such a historical label, current/proposed names must yield one parent.
    The independently supplied rename parent must still agree with that result.
    """
    sid = state['id']
    if move is None:
        return _record_path(rename)
    path = _record_path(move)
    expected = _record_path(rename) if rename else None
    def strip_title(title):
        if not isinstance(title, str) or not title or not path.endswith('/' + title):
            return None
        parent = path[:-(len(title) + 1)]
        return _path(parent) if parent else None
    parent = None
    labels = [move['row'].get('Current item')]
    if rename:
        labels.append(rename['row'].get('Current name + type suffix'))
    for label in labels:
        parent = strip_title(label)
        if parent is not None:
            break
    if parent is None:
        alternatives = {state['name']}
        if rename:
            alternatives.update({rename['row'].get('Proposed name + type suffix'), exact_rename(rename, state)})
        candidates = {p for title in alternatives if (p := strip_title(title)) is not None}
        if len(candidates) > 1:
            raise SafetyError('AMBIGUOUS_FILE_TITLE_PATH:' + sid)
        parent = next(iter(candidates)) if candidates else logical_parent(path)
    if rename and parent != expected:
        raise SafetyError('FILE_MOVE_RENAME_DESTINATION_DISAGREEMENT:' + sid)
    return parent


def select_destination_records(source, section_path):
    """Normal prefix selection plus explicit top-level folder MOVE/RENAME pairs.

    A blank rename destination alone is ambiguous. It must be paired with an
    exact top-level MOVE row before the configured root can give it meaning.
    """
    section = _path(section_path)
    if '/' in section:
        return nested_select_records(source, section)
    blocked = _blocked_ids(source)
    roots = {r['source_id'] for r in source['records'].values()
             if r['action'] == 'MOVE' and r['row'].get('Exact target path') == section
             and r['source_id'] not in blocked}
    additions = []
    for r in source['records'].values():
        if (r['action'] != 'RENAME' or r['row'].get('Proposed destination folder') != ''
                or r['source_id'] in blocked):
            continue
        if r['source_id'] not in roots:
            if r['row'].get('Proposed name + type suffix') == section:
                raise SafetyError('TOP_LEVEL_FOLDER_RENAME_REQUIRES_EXPLICIT_MOVE_ROW:' + r['source_id'])
            continue
        if r.get('decision') not in {'', 'Yes'}:
            raise SafetyError('SECTION_DECISION_UNRECOGNIZED')
        additions.append(copy.deepcopy(r))
    records = nested_select_records(source, section) + additions
    if len({(r['source_id'], r['action']) for r in records}) != len(records):
        raise SafetyError('SECTION_DUPLICATE_SOURCE_ACTION')
    return sorted(records, key=lambda r: r['reference'])


def _blocked_ids(source):
    blocked = set(source.get('excluded_ids', []))
    refs = set(source.get('excluded_refs', []))
    for record in source['records'].values():
        if (record.get('withdrawn') or record.get('decision') == 'No'
                or record['reference'] in refs or set(record.get('proposal_refs', [])) & refs):
            blocked.add(record['source_id'])
    return blocked


def _folder_route(actions, state, required):
    move, rename = actions.get('MOVE'), actions.get('RENAME')
    if move:
        path = _record_path(move)
        if rename and _record_path(rename) != logical_parent(path):
            raise SafetyError('FOLDER_MOVE_RENAME_PARENT_DISAGREEMENT:' + state['id'])
    else:
        historical = rename['row'].get('Current name + type suffix') or state['name']
        old = _record_path(rename) + '/' + historical
        current = _record_path(rename) + '/' + state['name']
        new = _record_path(rename) + '/' + exact_rename(rename, state)
        referenced = {p for p in {old, current, new} if any(_inside(r, p) for r in required)}
        if len(referenced) > 1:
            raise SafetyError('AMBIGUOUS_RENAMED_FOLDER_LOGICAL_ALIAS:' + state['id'])
        path = next(iter(referenced)) if referenced else new
    return path, exact_rename(rename, state) if rename else path.rsplit('/', 1)[-1]


def _reusable(sid, states, routes, blocked):
    state = states[sid]
    subtree = state.get('subtree')
    if not isinstance(subtree, dict) or subtree.get('complete') is not True:
        return None, 'INCOMPLETE_LIVE_SUBTREE'
    members = subtree.get('members', [])
    ids = {m['id'] for m in members}
    if len(ids) != len(members) or sid in ids:
        raise SafetyError('INVALID_DESCENDANT_SCOPE:' + sid)
    known_direct = {child for child, observed in states.items() if sid in observed.get('parents', [])}
    if not known_direct <= ids:
        raise SafetyError('LIVE_SUBTREE_OMITS_KNOWN_CHILD:' + sid)
    if ids & blocked:
        return None, 'EXCLUDED_OR_WITHDRAWN_PASSENGER'
    if not ids <= routes.keys():
        return None, 'UNASSIGNED_OR_OUTSIDE_SECTION_PASSENGER'
    for member in members:
        child = member['id']
        observed = states[child]
        # Known names and parents from the subtree cannot contradict the source
        # snapshot used for mutation, even when this folder is not reused.
        for field in ('name', 'parents', 'mime_type'):
            if field in member and member[field] != observed.get(field):
                raise SafetyError('FOLDER_MEMBERSHIP_METADATA_CONFLICT:' + child)
        parents = observed['parents']
        if len(parents) != 1 or parents[0] not in ids | {sid}:
            return None, 'LIVE_RELATIVE_STRUCTURE_INCOMPLETE'
        parent = parents[0]
        parent_route = routes[parent]
        if states[parent]['mime_type'] != FOLDER or routes[child]['parent_path'] != parent_route['path']:
            return None, 'DESCENDANT_ASSIGNED_TO_DIFFERENT_ROUTE'
        seen = {child}
        cursor = parent
        while cursor != sid:
            if cursor in seen or len(states[cursor]['parents']) != 1:
                raise SafetyError('FOLDER_CYCLE:' + sid)
            seen.add(cursor)
            cursor = states[cursor]['parents'][0]
            if cursor not in ids | {sid}:
                return None, 'LIVE_RELATIVE_STRUCTURE_INCOMPLETE'
    proof = {'source_id': sid, 'logical_path': routes[sid]['path'],
             'descendant_ids': sorted(ids), 'subtree_sha256': digest(subtree),
             'route_sha256': digest({i: routes[i] for i in sorted(ids | {sid})})}
    return proof, None


def plan_destination(source, *, section_path, metadata_by_id, anchor_paths):
    """Return exact routes, proved reuse, and required destinations.

    ``anchor_paths`` contains independently verified existing destinations,
    including the section's immediate parent. Missing destinations have stable
    logical keys and no invented IDs. The caller resolves exact live matches or
    creates these containers, then calls this function again with their IDs.
    Folder subtree members must be recursive; all mutation candidates require
    exact metadata. No descendant is inferred eligible from a broad folder row.
    """
    section = _path(section_path)
    anchor = logical_parent(section)
    blocked = _blocked_ids(source)
    records = [r for r in select_destination_records(source, section) if r['source_id'] not in blocked]
    if not records:
        raise SafetyError('SECTION_NO_ELIGIBLE_RECORDS')
    groups, states = {}, {}
    for record in records:
        sid = record['source_id']
        if sid not in metadata_by_id:
            raise SafetyError('SOURCE_METADATA_REQUIRED:' + sid)
        states[sid] = copy.deepcopy(metadata_by_id[sid])
        if states[sid].get('id') != sid:
            raise SafetyError('SOURCE_ID_MISMATCH:' + sid)
        groups.setdefault(sid, {})[record['action']] = record
    required = {section}
    for sid, actions in groups.items():
        if states[sid]['mime_type'] != FOLDER:
            required.add(file_parent_path(actions.get('MOVE'), actions.get('RENAME'), states[sid]))
        else:
            for record in actions.values():
                required.add(logical_parent(_record_path(record)) if record['action'] == 'MOVE' else _record_path(record))
    routes, names = {}, {}
    for sid, actions in sorted(groups.items()):
        state = states[sid]
        rename, move = actions.get('RENAME'), actions.get('MOVE')
        if state['mime_type'] == FOLDER:
            path, name = _folder_route(actions, state, required)
            if not _inside(path, section):
                raise SafetyError('FOLDER_TARGET_OUTSIDE_SECTION:' + sid)
            if path in names and names[path] != name:
                raise SafetyError('CONSOLIDATED_DESTINATION_NAME_DISAGREEMENT:' + path)
            names[path] = name
            routes[sid] = {'path': path, 'parent_path': logical_parent(path), 'exact_name': name}
            required.add(path)
        else:
            parent = file_parent_path(move, rename, state)
            if not _inside(parent, section):
                raise SafetyError('FILE_DESTINATION_OUTSIDE_SECTION:' + sid)
            routes[sid] = {'parent_path': parent, 'exact_name': exact_rename(rename, state) if rename else state['name']}
    required.discard(anchor)
    for path in list(required):
        cursor = path
        while cursor != anchor:
            if not _inside(cursor, section):
                raise SafetyError('SECTION_PARENT_ANCHOR_UNRESOLVED:' + cursor)
            required.add(cursor)
            cursor = logical_parent(cursor)
    names = {p: names.get(p, p.rsplit('/', 1)[-1]) for p in required}
    paths = dict(anchor_paths)
    unused = set(paths) - (required | {anchor})
    if anchor not in paths or any(not _inside(path, section) for path in unused):
        raise SafetyError('DESTINATION_ANCHOR_OUTSIDE_SECTION_OR_MISSING')
    # Previous completed runs may retain empty destination folders whose routes
    # are no longer referenced. Keep their registry/history untouched, but do
    # not turn them into targets or require their metadata for this batch.
    paths = {path: sid for path, sid in paths.items() if path not in unused}
    if len(set(paths.values())) != len(paths):
        raise SafetyError('FOLDER_ID_HAS_MULTIPLE_TARGET_PATHS')
    for path, sid in paths.items():
        state = metadata_by_id.get(sid)
        if not state or state.get('id') != sid or state.get('mime_type') != FOLDER or state.get('trashed') is True:
            raise SafetyError('ANCHOR_FOLDER_METADATA_REQUIRED:' + path)
    proofs, reasons = {}, {}
    folders = {sid for sid in groups if states[sid]['mime_type'] == FOLDER}
    for sid in sorted(folders):
        proof, reason = _reusable(sid, states, routes, blocked)
        if proof:
            proofs[sid] = proof
        else:
            reasons[sid] = reason
    # An already verified destination wins over an incoming source container.
    # A reused ancestor is permitted only if all its descendant folders retain
    # their stable IDs at those exact logical routes too.
    while True:
        resolved = dict(paths)
        for sid in sorted(proofs):
            path = routes[sid]['path']
            if path in paths:
                continue
            if path in resolved and resolved[path] != sid:
                raise SafetyError('AMBIGUOUS_REUSABLE_FOLDERS:' + path)
            resolved[path] = sid
        selected = {sid for sid in proofs if resolved.get(routes[sid]['path']) == sid}
        bad = {sid for sid in selected if any(
            child in folders and resolved.get(routes[child]['path']) != child
            for child in proofs[sid]['descendant_ids'])}
        if not bad:
            break
        for sid in bad:
            del proofs[sid]
            reasons[sid] = 'DESCENDANT_FOLDER_REBOUND_TO_OTHER_ID'
    reuse = {sid: proofs[sid] for sid in selected}
    containers = folders - selected
    if containers & set(resolved.values()):
        raise SafetyError('MIXED_SOURCE_CANNOT_BE_MUTATED_DESTINATION')
    for sid in folders - selected:
        reasons.setdefault(sid, 'EXISTING_DESTINATION_REUSED_INSTEAD')
    # Same logical path may consolidate several old folders, but different
    # logical paths or file IDs cannot claim one exact final name in a parent.
    occupants = {}
    for path in sorted(required):
        key = (logical_parent(path), names[path])
        if key in occupants and occupants[key] != path:
            raise SafetyError('DESTINATION_FOLDER_NAME_COLLISION:' + path)
        occupants[key] = path
    for sid, route in sorted(routes.items()):
        if sid in folders:
            continue
        key = (route['parent_path'], route['exact_name'])
        if key in occupants:
            raise SafetyError('DESTINATION_EXACT_NAME_COLLISION:' + sid + ':' + str(occupants[key]))
        occupants[key] = sid
    missing = [{'logical_key': 'folder:' + digest({'section_path': section, 'logical_path': p}),
                'logical_path': p, 'folder_name': names[p], 'parent_path': logical_parent(p), 'kind': 'NEW_FOLDER_REQUIRED'}
               for p in sorted(required - resolved.keys(), key=lambda p: (p.count('/'), p))]
    return {'records': records, 'item_routes': routes, 'destination_paths': resolved,
            'destination_folder_names': names, 'missing_folders': missing,
            'reused_folder_ids': sorted(reuse), 'folder_reuse_proofs': reuse,
            'source_container_ids': sorted(containers),
            'source_container_refs': sorted(r['reference'] for r in records if r['source_id'] in containers),
            'source_container_reasons': reasons,
            'path_provenance': {p: {'kind': 'PROVED_INTACT_FOLDER_REUSE' if sid in reuse else 'VERIFIED_EXISTING_DESTINATION',
                                    'source_id': sid} for p, sid in resolved.items()}}


def build_hybrid_preparation(source, *, section_path, section_id, workbook_id, metadata_by_id,
                             destination_paths, destination_path_evidence_ref,
                             native_conversion_refs=None, extension_corrections=None):
    """Build importer-compatible field operations, preserving true dependencies."""
    layout = plan_destination(source, section_path=section_path, metadata_by_id=metadata_by_id, anchor_paths=destination_paths)
    if layout['missing_folders']:
        raise SafetyError('DESTINATION_CREATION_OR_MAPPING_REQUIRED')
    containers = set(layout['source_container_ids'])
    groups = {}
    for record in layout['records']:
        if record['source_id'] not in containers:
            groups.setdefault(record['source_id'], {})[record['action']] = record
    states = {sid: agent_snapshot(metadata_by_id[sid]) for sid in groups}
    for sid, actions in groups.items():
        if 'MOVE' not in actions:
            actions['MOVE'] = destination_route_record(actions['RENAME'])
    targets = {sid: [layout['destination_paths'][layout['item_routes'][sid]['parent_path']]] for sid in groups}
    ordered, deps = _order_groups(groups, states, targets)
    operations, resolutions, last_ref = [], {}, {}
    for sid in ordered:
        previous = [last_ref[p] for p in sorted(deps[sid])]
        for action in ('RENAME', 'MOVE'):
            record = groups[sid].get(action)
            if record is None:
                continue
            ref = record['reference']
            op = {'reference': ref, 'source_id': sid, 'action': action,
                  'depends_on_refs': sorted(previous), 'allowed_collision_ids': []}
            if action == 'MOVE':
                op['target_parent_ids'] = targets[sid]
                if record.get('derived_from_reference'):
                    op['derived_from_reference'] = record['derived_from_reference']
            else:
                op['exact_target_name'] = exact_rename(record, states[sid])
                if op['exact_target_name'] != record['row']['Proposed name + type suffix']:
                    proof = (native_conversion_refs or {}).get(ref)
                    if not proof:
                        raise SafetyError('VERIFIED_NATIVE_DISPLAY_CONVERSION_REQUIRED')
                    op['native_display_conversion_ref'] = proof
                if ref in (extension_corrections or {}):
                    op['extension_correction'] = copy.deepcopy(extension_corrections[ref])
            resolutions[ref] = {'input_sha256': source['input_sha256'], 'row_sha256': record['row_sha256'],
                               'source_id': sid, 'live_snapshot_sha256': digest(states[sid]), 'reviewed_checks': True,
                               **{k: copy.deepcopy(v) for k, v in op.items() if k not in {'reference', 'source_id', 'action'}}}
            operations.append(op)
            previous, last_ref[sid] = [ref], ref
    inherited = {sid: [] for sid in groups}
    for folder in layout['reused_folder_ids']:
        move_ref = groups[folder]['MOVE']['reference']
        for child in layout['folder_reuse_proofs'][folder]['descendant_ids']:
            inherited[child].append(move_ref)
    revision = 'hybrid:' + digest({'input': source['input_sha256'], 'section': section_path,
                                   'destinations': layout['destination_paths'], 'operations': operations})
    projection = {'schema_version': 'uqmc.destination.section.v1', 'section_id': section_id,
                  'section_path': section_path, 'workbook_id': workbook_id, 'input_sha256': source['input_sha256'],
                  'projection_revision': revision, 'model_revision': revision, 'method_profile': PROFILE,
                  'complete': True, 'allowed_actions': sorted({o['action'] for o in operations}),
                  'allowed_destination_parent_ids': sorted({p for o in operations if o['action'] == 'MOVE' for p in o['target_parent_ids']}),
                  'destination_paths': layout['destination_paths'], 'destination_path_evidence_ref': destination_path_evidence_ref,
                  'destination_folder_names': layout['destination_folder_names'],
                  'reused_folder_ids': layout['reused_folder_ids'], 'folder_reuse_proofs': layout['folder_reuse_proofs'],
                  'source_container_ids': layout['source_container_ids'], 'source_container_refs': layout['source_container_refs'],
                  'declared_member_count': len(groups),
                  'members': [{'source_id': sid, 'direct_operation_refs': sorted(o['reference'] for o in operations if o['source_id'] == sid),
                               'ancestor_move_operation_refs': sorted(inherited[sid])} for sid in sorted(groups)],
                  'operations': operations}
    return {'projection': projection, 'resolutions': resolutions, 'selected_refs': [o['reference'] for o in operations],
            'source_ids': sorted(groups)}
