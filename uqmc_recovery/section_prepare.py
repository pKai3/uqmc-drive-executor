"""Deterministic, offline preparation of one exact destination section.

The caller captures the workbook and metadata, seals path provenance and owner
instructions, then passes this result through import_section. These helpers do
not grant approval, access Drive, create folders or modify the journal.
"""
from __future__ import annotations

import copy
from .agent_profile import agent_snapshot
from .model import FOLDER, NATIVE, SafetyError, digest


def _path(value):
    if not isinstance(value, str) or not value or value.startswith('/') or value.endswith('/'):
        raise SafetyError('EXACT_LOGICAL_PATH_REQUIRED')
    if any(part in {'', '.', '..'} for part in value.split('/')):
        raise SafetyError('AMBIGUOUS_LOGICAL_PATH')
    return value


def _inside(path, section):
    return path == section or path.startswith(section + '/')


def _parent(path):
    if '/' not in path:
        raise SafetyError('LOGICAL_PARENT_REQUIRED')
    return path.rsplit('/', 1)[0]


def _record_path(record):
    field = 'Exact target path' if record['action'] == 'MOVE' else 'Proposed destination folder'
    return _path(record['row'].get(field))


def select_records(source, section_path):
    """Select exact path-prefix records; exclusions override section approval."""
    section = _path(section_path)
    excluded = set(source.get('excluded_ids', []))
    excluded_refs = set(source.get('excluded_refs', []))
    selected = []
    for record in source['records'].values():
        if record.get('action') not in {'MOVE', 'RENAME'}:
            continue
        value = record['row'].get('Exact target path' if record['action'] == 'MOVE' else 'Proposed destination folder')
        if not isinstance(value, str) or not value:
            continue
        path = value
        matches = _inside(path, section)
        # A folder renamed at the immediate parent can form the section root;
        # its MIME and exact path are subsequently verified, never assumed.
        if record['action'] == 'RENAME' and path + '/' + str(record['row'].get('Proposed name + type suffix', '')) == section:
            matches = True
        if not matches:
            continue
        _path(path)
        if (record.get('withdrawn') or record.get('decision') == 'No' or record['source_id'] in excluded
                or record['reference'] in excluded_refs or set(record.get('proposal_refs', [])) & excluded_refs):
            continue
        if record.get('decision') not in {'', 'Yes'}:
            raise SafetyError('SECTION_DECISION_UNRECOGNIZED')
        selected.append(copy.deepcopy(record))
    if not selected:
        raise SafetyError('SECTION_NO_ELIGIBLE_RECORDS')
    if len({r['reference'] for r in selected}) != len(selected):
        raise SafetyError('SECTION_DUPLICATE_REFERENCE')
    actions = [(r['source_id'], r['action']) for r in selected]
    if len(set(actions)) != len(actions):
        raise SafetyError('SECTION_DUPLICATE_SOURCE_ACTION')
    return sorted(selected, key=lambda r: r['reference'])


def exact_rename(record, state):
    title = record['row'].get('Proposed name + type suffix')
    if not isinstance(title, str) or not title:
        raise SafetyError('WORKBOOK_PROPOSED_RENAME_REQUIRED')
    suffix = NATIVE.get(state['mime_type'])
    # Only the format's known display suffix is removed. Uploaded extensions
    # and literal folder names are never altered here.
    if suffix and title.endswith(suffix):
        title = title[:-len(suffix)]
    if not title:
        raise SafetyError('EXACT_NATIVE_TITLE_REQUIRED')
    return title


def resolve_destination_paths(records, metadata_by_id, anchor_paths, section_path, *, method_profile="approved_nested_section"):
    """Bind reused folders by ID and return only necessary missing containers.

    anchor_paths is caller-verified metadata provenance, not a display-node map.
    Missing specs contain no invented ID and are ordered parent before child.
    """
    if method_profile == "destination_scoped_files":
        return _destination_layout(records, metadata_by_id, anchor_paths, section_path)
    if method_profile != "approved_nested_section":
        raise SafetyError("SECTION_METHOD_PROFILE_UNSUPPORTED")
    section = _path(section_path)
    paths = dict(anchor_paths)
    provenance = {p: {'kind': 'VERIFIED_EXISTING_ANCHOR', 'source_id': sid} for p, sid in paths.items()}
    for path, sid in paths.items():
        _path(path)
        if not isinstance(sid, str) or not sid:
            raise SafetyError('DESTINATION_ID_REQUIRED')
        state = metadata_by_id.get(sid)
        if state is None or state.get('id') != sid or state.get('mime_type') != FOLDER:
            raise SafetyError('ANCHOR_FOLDER_METADATA_REQUIRED')
    grouped = {}
    required = {section}
    for record in records:
        grouped.setdefault(record['source_id'], {})[record['action']] = record
        required.add(_parent(_record_path(record)) if record['action'] == 'MOVE' else _record_path(record))
    for sid, actions in grouped.items():
        state = metadata_by_id[sid]
        if state.get('id') != sid:
            raise SafetyError('SOURCE_ID_MISMATCH')
        if state.get('mime_type') != FOLDER:
            # A nonfolder cannot masquerade as the section root rename.
            if any(not _inside(_record_path(r), section) for r in actions.values()):
                raise SafetyError('SECTION_ROOT_MUST_BE_FOLDER')
            continue
        move = actions.get('MOVE')
        rename = actions.get('RENAME')
        move_path = _record_path(move) if move else None
        rename_path = _record_path(rename) + '/' + exact_rename(rename, state) if rename else None
        if move_path and rename_path and _parent(move_path) != _parent(rename_path):
            raise SafetyError('FOLDER_MOVE_RENAME_PARENT_DISAGREEMENT')
        # Spreadsheet destination segments are logical collection keys. A
        # separate folder rename does not change which stable ID that key names.
        # Descendant rows can still use the old collection label.
        if move_path:
            path = move_path
        else:
            current_path = _record_path(rename) + '/' + state['name']
            candidates = {current_path, rename_path}
            referenced = {candidate for candidate in candidates
                          if any(_inside(required_path, candidate) for required_path in required)}
            if len(referenced) > 1:
                raise SafetyError('AMBIGUOUS_RENAMED_FOLDER_LOGICAL_ALIAS')
            path = next(iter(referenced)) if referenced else current_path
        if not _inside(path, section):
            raise SafetyError('FOLDER_TARGET_OUTSIDE_SECTION')
        if path in paths and paths[path] != sid:
            raise SafetyError('DESTINATION_PATH_HAS_MULTIPLE_IDENTITIES')
        if sid in paths.values() and any(p != path and value == sid for p, value in paths.items()):
            raise SafetyError('FOLDER_ID_HAS_MULTIPLE_TARGET_PATHS')
        paths[path] = sid
        provenance[path] = {'kind': 'EXPLICIT_SOURCE_FOLDER_REUSE', 'source_id': sid,
                            'proposal_refs': sorted(r['reference'] for r in actions.values())}
        required.add(path)
    # All required descendants have an explicit route to the verified anchor.
    for path in list(required):
        cursor = path
        while cursor not in paths:
            if not _inside(cursor, section):
                raise SafetyError('SECTION_PARENT_ANCHOR_UNRESOLVED')
            required.add(cursor)
            cursor = _parent(cursor)
    missing = []
    for path in sorted(required - paths.keys(), key=lambda p: (p.count('/'), p)):
        missing.append({'logical_key': 'folder:' + digest({'section_path': section, 'logical_path': path}),
                        'logical_path': path, 'folder_name': path.rsplit('/', 1)[-1],
                        'parent_path': _parent(path), 'kind': 'NEW_FOLDER_REQUIRED'})
    return {'destination_paths': paths, 'path_provenance': provenance, 'missing_folders': missing}


def _order_groups(groups, states, target_parents):
    deps = {sid: {p for p in target_parents[sid] if p in groups and states[p]['mime_type'] == FOLDER} for sid in groups}
    def depends(sid, ancestor, seen=None):
        seen = set() if seen is None else seen
        if sid in seen:
            return False
        seen.add(sid)
        return any(d == ancestor or depends(d, ancestor, seen) for d in deps[sid])
    for sid in sorted(groups):
        if states[sid]['mime_type'] != FOLDER:
            continue
        descendants = {m['id'] for m in states[sid]['subtree']['members']}
        descendants |= {child for child in groups if sid in states[child]['parents']}
        for child in sorted(descendants & groups.keys()):
            if child == sid:
                raise SafetyError('FOLDER_CYCLE')
            if not depends(sid, child):
                deps[child].add(sid)
    ordered, pending = [], set(groups)
    while pending:
        ready = sorted(s for s in pending if deps[s] <= set(ordered))
        if not ready:
            raise SafetyError('TARGET_FOLDER_CYCLE')
        ordered.extend(ready)
        pending.difference_update(ready)
    return ordered, deps


def build_preparation(source, *, section_path, section_id, workbook_id, metadata_by_id,
                      destination_paths, destination_path_evidence_ref,
                      native_conversion_refs=None, extension_corrections=None,
                      method_profile="approved_nested_section"):
    """Build exact projection/resolutions; caller seals owner approval afterwards."""
    if method_profile == "destination_scoped_files":
        return _build_destination_preparation(source, section_path=section_path, section_id=section_id,
            workbook_id=workbook_id, metadata_by_id=metadata_by_id, destination_paths=destination_paths,
            destination_path_evidence_ref=destination_path_evidence_ref, native_conversion_refs=native_conversion_refs,
            extension_corrections=extension_corrections)
    if method_profile != "approved_nested_section":
        raise SafetyError("SECTION_METHOD_PROFILE_UNSUPPORTED")
    records = select_records(source, section_path)
    native_conversion_refs, extension_corrections = native_conversion_refs or {}, extension_corrections or {}
    states = {r['source_id']: agent_snapshot(metadata_by_id[r['source_id']]) for r in records}
    groups = {}
    for r in records:
        groups.setdefault(r['source_id'], {})[r['action']] = r
    member_ids = set(groups)
    selected_refs = {r['reference'] for r in records}
    excluded = set(source.get('excluded_ids', []))
    outside_moves = {r['source_id'] for r in source['records'].values()
                     if r['action'] == 'MOVE' and r['reference'] not in selected_refs}
    inherited = {sid: [] for sid in groups}
    for sid, actions in groups.items():
        if states[sid]['mime_type'] != FOLDER:
            continue
        descendants = {m['id'] for m in states[sid]['subtree']['members']}
        if descendants & excluded:
            raise SafetyError('EXCLUDED_FOLDER_DESCENDANT')
        unknown = descendants - member_ids
        scope = actions.get('MOVE', {}).get('row', {}).get('Scope', '').lower()
        if unknown and (not scope.startswith('whole subtree') or unknown & outside_moves):
            raise SafetyError('SECTION_FOLDER_PARTITION_NOT_COMPLETE')
        member_ids |= unknown
        for child in descendants:
            inherited.setdefault(child, [])
            if 'MOVE' in actions:
                inherited[child].append(actions['MOVE']['reference'])
    targets = {}
    for sid, actions in groups.items():
        if 'MOVE' in actions:
            parent_path = _parent(_record_path(actions['MOVE']))
            if parent_path not in destination_paths:
                raise SafetyError('DESTINATION_CREATION_OR_MAPPING_REQUIRED')
            targets[sid] = [destination_paths[parent_path]]
        else:
            targets[sid] = states[sid]['parents']
    ordered, dependencies = _order_groups(groups, states, targets)
    operations, resolutions, last_ref = [], {}, {}
    for sid in ordered:
        previous = [last_ref[parent] for parent in sorted(dependencies[sid])]
        for action in ('RENAME', 'MOVE'):
            record = groups[sid].get(action)
            if record is None:
                continue
            ref = record['reference']
            operation = {'reference': ref, 'source_id': sid, 'action': action,
                         'depends_on_refs': sorted(previous), 'allowed_collision_ids': []}
            if action == 'MOVE':
                operation['target_parent_ids'] = targets[sid]
            else:
                operation['exact_target_name'] = exact_rename(record, states[sid])
                proposed = record['row']['Proposed name + type suffix']
                if proposed != operation['exact_target_name']:
                    proof = native_conversion_refs.get(ref)
                    if not proof:
                        raise SafetyError('VERIFIED_NATIVE_DISPLAY_CONVERSION_REQUIRED')
                    operation['native_display_conversion_ref'] = proof
                if ref in extension_corrections:
                    operation['extension_correction'] = copy.deepcopy(extension_corrections[ref])
            resolution = {'input_sha256': source['input_sha256'], 'row_sha256': record['row_sha256'],
                          'source_id': sid, 'live_snapshot_sha256': digest(states[sid]), 'reviewed_checks': True,
                          **{key: copy.deepcopy(value) for key, value in operation.items()
                             if key not in {'reference', 'source_id', 'action'}}}
            operations.append(operation)
            resolutions[ref] = resolution
            previous, last_ref[sid] = [ref], ref
    revision = 'nested:' + digest({'input': source['input_sha256'], 'section': section_path,
                                  'destinations': destination_paths, 'operations': operations})
    projection = {'schema_version': 'uqmc.destination.section.v1', 'section_id': section_id,
                  'section_path': section_path, 'workbook_id': workbook_id, 'input_sha256': source['input_sha256'],
                  'projection_revision': revision, 'model_revision': revision,
                  'method_profile': 'approved_nested_section', 'complete': True,
                  'allowed_actions': sorted({r['action'] for r in records}),
                  'allowed_destination_parent_ids': sorted({p for op in operations if op['action'] == 'MOVE' for p in op['target_parent_ids']}),
                  'destination_paths': copy.deepcopy(destination_paths),
                  'destination_path_evidence_ref': destination_path_evidence_ref,
                  'declared_member_count': len(member_ids),
                  'members': [{'source_id': sid, 'direct_operation_refs': sorted(r['reference'] for r in records if r['source_id'] == sid),
                               'ancestor_move_operation_refs': sorted(inherited.get(sid, []))} for sid in sorted(member_ids)],
                  'operations': operations}
    return {'projection': projection, 'resolutions': resolutions, 'selected_refs': [op['reference'] for op in operations]}


def _destination_layout(records, metadata_by_id, anchor_paths, section_path):
    """Use source folder rows as destination structure, never mutation identity.

    Logical labels may retain old names while the required destination's actual
    title comes from its exact folder rename row. Unrelated current children do
    not participate in destination-scoped selection.
    """
    section = _path(section_path)
    containers = {r['source_id'] for r in records if metadata_by_id[r['source_id']]['mime_type'] == FOLDER}
    container_refs = sorted(r['reference'] for r in records if r['source_id'] in containers)
    if containers & set(anchor_paths.values()):
        raise SafetyError('SOURCE_CONTAINER_CANNOT_BE_DESTINATION')
    anchor = _parent(section)
    if anchor not in anchor_paths:
        raise SafetyError('SECTION_PARENT_ANCHOR_UNRESOLVED')
    # Derive logical aliases without carrying source identities into the
    # target map. Multiple old containers may consolidate into one destination
    # when their approved exact destination names agree.
    required = {section}
    grouped = {}
    for record in records:
        grouped.setdefault(record['source_id'], {})[record['action']] = record
        required.add(_parent(_record_path(record)) if record['action'] == 'MOVE' else _record_path(record))
    named = {}
    for sid in sorted(containers):
        actions, state = grouped[sid], metadata_by_id[sid]
        move, rename = actions.get('MOVE'), actions.get('RENAME')
        move_path = _record_path(move) if move else None
        rename_path = _record_path(rename) + '/' + exact_rename(rename, state) if rename else None
        if move_path and rename_path and _parent(move_path) != _parent(rename_path):
            raise SafetyError('FOLDER_MOVE_RENAME_PARENT_DISAGREEMENT')
        if move_path:
            path = move_path
        else:
            current_path = _record_path(rename) + '/' + state['name']
            candidates = {current_path, rename_path}
            referenced = {candidate for candidate in candidates if any(_inside(p, candidate) for p in required)}
            if len(referenced) > 1:
                raise SafetyError('AMBIGUOUS_RENAMED_FOLDER_LOGICAL_ALIAS')
            path = next(iter(referenced)) if referenced else current_path
        if not _inside(path, section):
            raise SafetyError('FOLDER_TARGET_OUTSIDE_SECTION')
        name = exact_rename(rename, state) if rename else path.rsplit('/', 1)[-1]
        if path in named and named[path] != name:
            raise SafetyError('CONSOLIDATED_DESTINATION_NAME_DISAGREEMENT')
        named[path] = name
        required.add(path)
    for path in list(required):
        cursor = path
        while cursor != anchor:
            if not _inside(cursor, section):
                raise SafetyError('SECTION_PARENT_ANCHOR_UNRESOLVED')
            required.add(cursor)
            cursor = _parent(cursor)
    required.discard(anchor)
    names = {path: named.get(path, path.rsplit('/', 1)[-1]) for path in required}
    paths = dict(anchor_paths)
    if set(paths) - (required | {anchor}):
        raise SafetyError('DESTINATION_ANCHOR_OUTSIDE_SECTION')
    for path, sid in paths.items():
        state = metadata_by_id.get(sid)
        if not state or state.get('id') != sid or state.get('mime_type') != FOLDER:
            raise SafetyError('ANCHOR_FOLDER_METADATA_REQUIRED')
    missing = [{'logical_key': 'folder:' + digest({'section_path': section, 'logical_path': path}),
                'logical_path': path, 'folder_name': names[path], 'parent_path': _parent(path),
                'kind': 'NEW_FOLDER_REQUIRED'}
               for path in sorted(required - paths.keys(), key=lambda p: (p.count('/'), p))]
    return {'destination_paths': paths, 'destination_folder_names': names,
            'source_container_ids': sorted(containers), 'source_container_refs': container_refs,
            'path_provenance': {p: {'kind': 'VERIFIED_DESTINATION_FOLDER', 'source_id': sid} for p, sid in paths.items()},
            'missing_folders': missing}


def _build_destination_preparation(source, *, section_path, section_id, workbook_id, metadata_by_id,
                                   destination_paths, destination_path_evidence_ref,
                                   native_conversion_refs=None, extension_corrections=None):
    from .importer import destination_route_record
    records = select_records(source, section_path)
    layout = _destination_layout(records, metadata_by_id, destination_paths, section_path)
    if layout['missing_folders']:
        raise SafetyError('DESTINATION_CREATION_OR_MAPPING_REQUIRED')
    containers = set(layout['source_container_ids'])
    native_conversion_refs, extension_corrections = native_conversion_refs or {}, extension_corrections or {}
    groups = {}
    for record in records:
        if record['source_id'] not in containers:
            groups.setdefault(record['source_id'], {})[record['action']] = record
    if not groups:
        raise SafetyError('SECTION_NO_ASSIGNED_FILES')
    operations, resolutions = [], {}
    for sid in sorted(groups):
        actions = groups[sid]
        state = agent_snapshot(metadata_by_id[sid])
        if state['mime_type'] == FOLDER:
            raise SafetyError('DESTINATION_SCOPED_FORBIDS_SOURCE_FOLDER_MUTATION')
        rename, move = actions.get('RENAME'), actions.get('MOVE')
        parent_path = _parent(_record_path(move)) if move else _record_path(rename)
        if rename and _record_path(rename) != parent_path:
            raise SafetyError('FILE_MOVE_RENAME_DESTINATION_DISAGREEMENT')
        if not _inside(parent_path, section_path):
            raise SafetyError('FILE_DESTINATION_OUTSIDE_SECTION')
        if parent_path not in destination_paths:
            raise SafetyError('DESTINATION_CREATION_OR_MAPPING_REQUIRED')
        if move is None:
            move = destination_route_record(rename)
        previous = []
        for action, record in [('RENAME', rename), ('MOVE', move)]:
            if record is None:
                continue
            ref = record['reference']
            op = {'reference': ref, 'source_id': sid, 'action': action,
                  'depends_on_refs': previous, 'allowed_collision_ids': []}
            if action == 'MOVE':
                op['target_parent_ids'] = [destination_paths[parent_path]]
                if record.get('derived_from_reference'):
                    op['derived_from_reference'] = record['derived_from_reference']
            else:
                op['exact_target_name'] = exact_rename(record, state)
                if op['exact_target_name'] != record['row']['Proposed name + type suffix']:
                    if ref not in native_conversion_refs:
                        raise SafetyError('VERIFIED_NATIVE_DISPLAY_CONVERSION_REQUIRED')
                    op['native_display_conversion_ref'] = native_conversion_refs[ref]
                if ref in extension_corrections:
                    op['extension_correction'] = copy.deepcopy(extension_corrections[ref])
            resolutions[ref] = {'input_sha256': source['input_sha256'], 'row_sha256': record['row_sha256'],
                               'source_id': sid, 'live_snapshot_sha256': digest(state), 'reviewed_checks': True,
                               **{k: copy.deepcopy(v) for k, v in op.items() if k not in {'reference', 'source_id', 'action'}}}
            operations.append(op)
            previous = [ref]
    revision = 'destination:' + digest({'input': source['input_sha256'], 'section': section_path,
        'destinations': destination_paths, 'folder_names': layout['destination_folder_names'], 'operations': operations})
    projection = {'schema_version': 'uqmc.destination.section.v1', 'section_id': section_id,
        'section_path': section_path, 'workbook_id': workbook_id, 'input_sha256': source['input_sha256'],
        'projection_revision': revision, 'model_revision': revision, 'method_profile': 'destination_scoped_files',
        'complete': True, 'allowed_actions': sorted({o['action'] for o in operations}),
        'allowed_destination_parent_ids': sorted({p for o in operations if o['action']=='MOVE' for p in o['target_parent_ids']}),
        'destination_paths': copy.deepcopy(destination_paths), 'destination_path_evidence_ref': destination_path_evidence_ref,
        'destination_folder_names': layout['destination_folder_names'],
        'source_container_ids': layout['source_container_ids'], 'source_container_refs': layout['source_container_refs'],
        'declared_member_count': len(groups),
        'members': [{'source_id': sid, 'direct_operation_refs': sorted(o['reference'] for o in operations if o['source_id']==sid),
                    'ancestor_move_operation_refs': []} for sid in sorted(groups)],
        'operations': operations}
    return {'projection': projection, 'resolutions': resolutions,
            'selected_refs': [o['reference'] for o in operations], 'source_ids': sorted(groups)}
