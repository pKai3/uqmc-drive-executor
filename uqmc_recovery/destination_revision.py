"""Explicit, sheet-bound new work on previously completed destination identities.

A revision manifest is private review input, never approval by itself. The
operator's --apply freezes its exact targets and fresh observed preimages in a
new plan. Old completions and uncertain requests are never rewritten.
"""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from .model import FOLDER, Operation, Plan, SafetyError, digest
from .section_prepare import _path, _inside, _order_groups, exact_rename
from .section_execution import collect_metadata, normalize, authorize
from .destination_history import build_history_index, reconcile_item
from .destination_planner import select_destination_records, file_parent_path, logical_parent

SCHEMA = 'uqmc.destination.revision.v1'


def read_manifest(value, source, section, workbook_id):
    value = copy.deepcopy(value if isinstance(value, dict) else json.loads(Path(value).read_text()))
    if (value.get('schema_version') != SCHEMA or value.get('section_path') != section
            or value.get('workbook_id') != workbook_id or not value.get('revision_id')
            or value.get('input_sha256') != source['input_sha256']):
        raise SafetyError('REVISION_MANIFEST_FRESH_SHEET_BINDING_MISMATCH')
    if value.get('expires_at_utc') is not None:
        from .section_importer import _time
        if _time(value['expires_at_utc']) <= datetime.now(timezone.utc):
            raise SafetyError('SECTION_APPROVAL_EXPIRED')
    items = value.get('items')
    if not isinstance(items, list) or not items or len({x.get('source_id') for x in items}) != len(items):
        raise SafetyError('REVISION_MANIFEST_IDENTITIES_INVALID')
    old, new = set(), set()
    for item in items:
        if not isinstance(item.get('source_id'), str) or not item['source_id']:
            raise SafetyError('REVISION_MANIFEST_ID_REQUIRED')
        for key in ('before_path', 'target_path'):
            if not _inside(_path(item.get(key)), section) or item[key] == section:
                raise SafetyError('REVISION_MANIFEST_OUTSIDE_SECTION')
        name = item.get('target_name')
        if not isinstance(name, str) or not name or not item['target_path'].endswith('/' + name):
            raise SafetyError('REVISION_MANIFEST_EXACT_NAME_REQUIRED')
        if item['before_path'] in old or item['target_path'] in new:
            raise SafetyError('REVISION_MANIFEST_PATH_COLLISION')
        old.add(item['before_path']); new.add(item['target_path'])
    folders = value.setdefault('new_folders', [])
    if not isinstance(folders, list) or len({x.get('path') for x in folders}) != len(folders):
        raise SafetyError('REVISION_NEW_FOLDERS_INVALID')
    for folder in folders:
        if (not _inside(_path(folder.get('path')), section) or folder['path'] == section
                or not folder.get('name') or not folder['path'].endswith('/' + folder['name'])):
            raise SafetyError('REVISION_NEW_FOLDER_SCOPE_INVALID')
    for folder in folders:
        if not any(_inside(target_parent(item), folder['path']) for item in items):
            raise SafetyError('REVISION_UNUSED_NEW_FOLDER:' + folder['path'])
    return value


def target_parent(item):
    return item['target_path'][:-len('/' + item['target_name'])]


def remap_paths(paths, manifest):
    """Stable IDs carry their entire logical subtree; longest explicit rule wins."""
    rules = sorted(manifest['items'], key=lambda x: len(x['before_path']), reverse=True)
    result = {}
    for path, sid in paths.items():
        rule = next((x for x in rules if _inside(path, x['before_path'])), None)
        target = rule['target_path'] + path[len(rule['before_path']):] if rule else path
        if target in result and result[target] != sid:
            raise SafetyError('REVISION_DESTINATION_ID_COLLISION:' + target)
        result[target] = sid
    return result


def _event_ref(event):
    return {'event_id': event['event_id'], 'sequence': event['seq'], 'record_sha256': event['hash']}


def _prior_proof(journal, sid, path, history, creation):
    h = history.get(sid)
    if h and h.get('latest_observation_ref'):
        return {'kind': 'COMPLETED_STATE', 'observed_ref': h['latest_observation_ref'],
                'prior_event_ids': h['event_ids'], 'prior_plan_sha256s': h['plan_hashes']}
    if sid in creation:
        proof = creation[sid]
        expected = 'folder:' + digest({'section_path': proof['creation_section_path'], 'logical_path': path})
        if proof['logical_key'] == expected:
            return {'kind': 'VERIFIED_CREATION', 'observed_ref': proof['observed_ref'], 'creation': proof}
    events = [e for e in journal.records() if e['kind'] == 'DESTINATION_VERIFIED'
              and e['payload'].get('file_id') == sid and e['payload'].get('proposal_path') == path]
    if events:
        event = events[-1]
        return {'kind': 'VERIFIED_DESTINATION', 'observed_ref': event['payload']['observed_ref'],
                'prior_event_ref': _event_ref(event)}
    raise SafetyError('REVISION_PRIOR_VERIFIED_IDENTITY_REQUIRED:' + sid)


def read_revision(config, section, journal, capture, factory, registry, report, manifest_input):
    from .importer import catalog, retired_source_ids
    from .destination import effective_blocked_sources, recursive_selected, semantic_selection
    from .destination_recovery import load_created_folder_proofs_for_path
    source = catalog(capture, expected_workbook_id=config['workbook_id'], include_subtree_coverage=True,
                     subtree_section_path=section)
    manifest = read_manifest(manifest_input, source, section, config['workbook_id'])
    records = journal.records()
    excluded = (set(config.get('excluded_ids', [])) | effective_blocked_sources(source)
                | {e['payload']['file_id'] for e in records if e['kind'] == 'EXCLUDED'} | retired_source_ids(records))
    entry = next((x for x in registry.get('sections', [])
                  if x.get('publication_spec', x).get('proposal_path') == section), None)
    if entry is None:raise SafetyError('REVISION_REQUIRES_REGISTERED_COMPLETED_SECTION')
    prior_spec = entry.get('publication_spec', entry)
    old_paths = {**config['anchor_paths'], **entry['destination_paths']}
    history = build_history_index(journal)
    creation = load_created_folder_proofs_for_path(journal, section)
    items = {x['source_id']: x for x in manifest['items']}
    if set(items) & excluded:raise SafetyError('REVISION_SOURCE_EXCLUDED')
    proofs = {}
    for sid, item in items.items():
        h = history.get(sid, {})
        if h.get('pending_requests'):raise SafetyError('REVISION_UNRESOLVED_PRIOR_REQUEST:' + sid)
        if h.get('rolled_back_fields'):raise SafetyError('REVISION_ROLLED_BACK_HISTORY_REQUIRES_RESUME:' + sid)
        # A completed repeat of this manifest is recognised by its target path.
        registered = old_paths.get(item['before_path']) == sid
        at_target = old_paths.get(item['target_path']) == sid
        previous = h.get('latest_observation')
        if previous and previous['mime_type'] != FOLDER:
            for path_key, flag in [('before_path', 'registered'), ('target_path', 'at_target')]:
                path = item[path_key]; suffix = '/' + previous['name']
                matches = path.endswith(suffix) and previous['parents'] == [old_paths.get(path[:-len(suffix)])]
                if flag == 'registered':registered = matches
                else:at_target = matches
        if not registered and not at_target:raise SafetyError('REVISION_RECORDED_PATH_ID_MISMATCH:' + sid)
        proofs[sid] = _prior_proof(journal, sid, item['before_path'] if registered else item['target_path'], history, creation)
    paths = remap_paths(old_paths, manifest)
    # Resolve only explicit stable-ID mappings; never adopt a same-name folder.
    for sid, item in items.items():
        if sid in old_paths.values():paths[item['target_path']] = sid
    folder_ids = {sid for path, sid in old_paths.items() if _inside(path, section)}
    selected = folder_ids | set(items)
    live = collect_metadata(selected, lambda allowed: factory(allowed, set(), excluded, False),
        excluded_ids=excluded, workers=config.get('workers', 10), progress=report, recursive_sources=False)
    recursive_selected(live)
    missing = []
    for folder in sorted(manifest['new_folders'], key=lambda x: x['path'].count('/')):
        path = folder['path']; parent = path[:-len('/' + folder['name'])]
        if path in paths:continue
        if parent not in paths and parent not in {x['logical_path'] for x in missing}:
            raise SafetyError('REVISION_NEW_PARENT_MAPPING_REQUIRED:' + parent)
        if parent in paths:
            collisions = [c for c in live['listings'].get(paths[parent], []) if c['name'] == folder['name']]
            if collisions:
                # A reserved identity may already exist after response loss.
                # Leave it in the missing specs: the established creator will
                # reconcile the exact allocation/marker before any retry.
                key = 'folder:' + digest({'section_path': section, 'logical_path': path})
                allocated = [e['payload'] for e in records if e['kind'] == 'REQUIRED_FOLDER_ALLOCATED'
                    and e['payload'].get('key') == key and e['payload'].get('name') == folder['name']
                    and e['payload'].get('parent_id') == paths[parent]]
                if len(allocated) != 1 or {c['id'] for c in collisions} != {allocated[0]['file_id']}:
                    raise SafetyError('REVISION_UNMAPPED_EXISTING_DESTINATION:' + path)
        missing.append({'logical_path': path, 'folder_name': folder['name'], 'parent_path': parent,
            'logical_key': 'folder:' + digest({'section_path': section, 'logical_path': path})})
    checks = []
    for sid, item in items.items():
        raw = live['raw'][sid]; h = history.get(sid)
        before = journal.read_evidence(proofs[sid]['observed_ref'])
        if h is None:
            before = {**before, 'mime_type': before.get('mime_type', before.get('mimeType'))}
            h = {'original': before, 'baseline': before, 'latest_observation': before,
                 'states': [before], 'completed_fields': {'name': before['name'], 'parents': before['parents']}}
        parent = target_parent(item)
        if parent not in paths and parent not in {x['logical_path'] for x in missing}:
            raise SafetyError('REVISION_TARGET_PARENT_NOT_BOUND:' + parent)
        target = {'id': sid, 'name': item['target_name'], 'parents': [paths.get(parent, 'uncreated-' + digest(parent)[:20])],
                  'mime_type': raw['mimeType']}
        check = reconcile_item(h, target, raw, revision_target=target)
        check['prior_completion'] = proofs[sid]
        checks.append(check)
        if parent in paths:
            for child in live['listings'].get(paths[parent], []):
                if child['name'] == item['target_name'] and child['id'] != sid:
                    check.update(disposition='BLOCKED', reason='REVISION_TARGET_NAME_COLLISION:' + child['id'])
    # Canonical file rows must still resolve to their same direct parent IDs and
    # names. Their path changes are inherited, not new child operations.
    groups = {}
    for record in select_destination_records(source, section):
        groups.setdefault(record['source_id'], {})[record['action']] = record
    declared_paths = set()
    for actions in groups.values():
        for record in actions.values():
            value = record['row'].get('Exact target path', record['row'].get('Proposed destination folder'))
            if isinstance(value, str):declared_paths.add(value)
    for item in items.values():
        if live['raw'][item['source_id']]['mimeType'] == FOLDER and not any(
                _inside(path, item['target_path']) for path in declared_paths):
            raise SafetyError('REVISION_FOLDER_TARGET_NOT_IN_FRESH_SHEET:' + item['source_id'])
    carried, seen = [], set()
    moved = [sid for sid in items if live['raw'][sid]['mimeType'] == FOLDER]
    for folder_id in moved:
        for member in live['metadata'][folder_id]['subtree']['members']:
            sid = member['id']
            if sid in items or sid in seen:continue
            seen.add(sid)
            if sid in excluded:raise SafetyError('REVISION_EXCLUDED_DESCENDANT:' + sid)
            if member.get('mime_type', member.get('mimeType')) == FOLDER:
                if sid not in paths.values():raise SafetyError('REVISION_UNMAPPED_DESCENDANT_FOLDER:' + sid)
                continue
            actions = groups.get(sid)
            if not actions:raise SafetyError('REVISION_DESCENDANT_MISSING_FRESH_PROPOSAL:' + sid)
            normal = normalize(member) if 'mimeType' in member else member
            parent_path = file_parent_path(actions.get('MOVE'), actions.get('RENAME'), normal)
            name = exact_rename(actions['RENAME'], normal) if actions.get('RENAME') else member['name']
            if paths.get(parent_path) not in member['parents'] or name != member['name']:
                raise SafetyError('REVISION_DESCENDANT_REQUIRES_SEPARATE_WORK:' + sid)
            h = history.get(sid, {})
            if h.get('pending_requests'):raise SafetyError('REVISION_UNRESOLVED_DESCENDANT_REQUEST:' + sid)
            if h.get('latest_observation'):
                latest = h['latest_observation']
                if latest['name'] != member['name'] or sorted(latest['parents']) != sorted(member['parents']):
                    raise SafetyError('REVISION_COMPLETED_DESCENDANT_DRIFT:' + sid)
            carried.append({'source_id': sid, 'name': member['name'], 'parents': member['parents'],
                            'target_parent_path': parent_path, 'prior_observation_ref': h.get('latest_observation_ref'),
                            'prior_plan_sha256s': h.get('plan_hashes', []), 'effect': 'INHERITED_PATH_ONLY'})
    # Explicit file revisions also require the exact fresh proposed target.
    for sid, item in items.items():
        if live['raw'][sid]['mimeType'] == FOLDER:continue
        actions = groups.get(sid, {}); normal = live['metadata'][sid]
        if (not actions or file_parent_path(actions.get('MOVE'), actions.get('RENAME'), normal) != target_parent(item)
                or (exact_rename(actions['RENAME'], normal) if actions.get('RENAME') else normal['name']) != item['target_name']):
            raise SafetyError('REVISION_FILE_TARGET_NOT_IN_FRESH_SHEET:' + sid)
    return {'source': source, 'capture': capture, 'manifest': manifest, 'prior_spec': prior_spec,
        'prior_paths': old_paths, 'prior_proofs': proofs, 'carried_descendants': carried,
        'live': live, 'checks': checks, 'excluded': excluded,
        'semantic_selection': semantic_selection(source, section),
        'layout': {'missing_folders': missing, 'destination_paths': paths, 'reused_folder_ids': moved,
                   'source_container_ids': prior_spec.get('retained_source_folder_ids', [])}}


def freeze_revision(config, section, journal, state, factory, run_dir, report):
    from .destination import section_key, utc, recursive_selected
    from .folder_creation import prepare_required_folders
    from .agent_profile import register_agent_plan
    from .batch_source import _age
    from .__main__ import save_new
    _age({'captured_at_utc': state['capture']['exported_at_utc']})
    manifest = state['manifest']; sid = section_key(section); stamp = utc()
    approval_id = sid + '-revision-' + uuid.uuid4().hex
    instruction_ref = journal.evidence({'kind': 'TRUSTED_USER_INSTRUCTION', 'text': section,
        'provenance': 'Operator invoked --apply --revision-manifest for this exact fresh-sheet revision.',
        'revision_manifest_sha256': digest(manifest), 'access_policy': 'SCOPED_INHERITANCE_ACCEPTED'})
    paths = dict(state['layout']['destination_paths']); missing = state['layout']['missing_folders']
    keys = {s['logical_path']: s['logical_key'] for s in missing}
    specs = [{'key': s['logical_key'], 'name': s['folder_name'], **({'parent_key': keys[s['parent_path']]}
             if s['parent_path'] in keys else {'parent_id': paths[s['parent_path']]})} for s in missing]
    creation_ref = journal.evidence({'schema_version': 'uqmc.folder-create-authority.v1', 'decision': 'APPROVED',
        'section_id': sid, 'approval_id': approval_id, 'provenance_ref': instruction_ref,
        'access_policy': 'SCOPED_INHERITANCE_ACCEPTED', 'folders': specs})
    created = prepare_required_folders(journal, factory, specs=missing, paths=paths, excluded_ids=state['excluded'],
        authority_ref=creation_ref, workers=config.get('workers', 10), report=report)
    with journal.locked():
        live = state['live']
        for path, raw in created.items():
            paths[path] = raw['id']; live['raw'][raw['id']] = raw; live['listings'].setdefault(raw['id'], [])
            live['metadata'][raw['id']] = normalize(raw, [])
            siblings = live['listings'].setdefault(raw['parents'][0], [])
            if raw['id'] not in {x['id'] for x in siblings}:siblings.append(copy.deepcopy(raw))
        recursive_selected(live)
        pending = {x['source_id'] for x in state['checks'] if x['disposition'] != 'COMPLETE'}
        items = {x['source_id']: x for x in manifest['items'] if x['source_id'] in pending}
        targets = {s: [paths[target_parent(x)]] for s, x in items.items()}
        order, dependencies = _order_groups(items, live['metadata'], targets)
        run_id = sid + '-revision-' + uuid.uuid4().hex[:12]
        operations, last = [], {}
        for source in order:
            before = copy.deepcopy(live['metadata'][source]); item = items[source]
            deps = tuple(last[d] for d in sorted(dependencies[source]))
            for action, field, value in [('RENAME', 'name', item['target_name']), ('MOVE', 'parents', targets[source])]:
                if before[field] == value:continue
                op_id = 'revision-' + digest({'run': run_id, 'source': source, 'action': action})[:24]
                operations.append(Operation(op_id, source, action, copy.deepcopy(before), {field: value}, deps,
                                            ('revision:' + manifest['revision_id'] + ':' + source,)))
                before[field] = copy.deepcopy(value); deps = (op_id,); last[source] = op_id
        if not operations:raise SafetyError('REVISION_HAS_NO_PENDING_OPERATIONS')
        plan = Plan(run_id, run_id, 'revision:' + digest(manifest), tuple(operations), tuple(sorted(state['excluded'])))
        register_agent_plan(journal, plan)
        owner = {'schema_version': 'uqmc.destination.revision-approval.v1', 'approval_id': approval_id,
            'decision': 'APPROVED', 'section_id': sid, 'manifest_sha256': digest(manifest), 'plan_sha256': plan.proposal_hash,
            'provenance_ref': instruction_ref, 'approved_at_utc': stamp,
            'expires_at_utc': manifest.get('expires_at_utc'),
            'operation_refs': [o.operation_id for o in operations]}
        owner_ref = journal.evidence(owner)
        source_ref = journal.evidence({'metadata': live['raw'], 'listings': live['listings'], 'permission_audit': False})
        evidence = {'schema_version': 'uqmc.destination.revision-frozen.v1', 'manifest': manifest,
            'workbook_export': state['capture'], 'plan': plan.body(), 'owner_approval_ref': owner_ref,
            'prior_completion_proofs': state['prior_proofs'], 'carried_descendants': state['carried_descendants'],
            'source_metadata_ref': source_ref, 'destination_paths': paths, 'prior_destination_paths': state['prior_paths']}
        manifest_ref = journal.evidence(evidence)
        journal.append('DESTINATION_REVISION_FROZEN', payload={'section_id': sid, 'run_id': run_id,
            'proposal_hash': plan.proposal_hash, 'manifest_ref': manifest_ref, 'owner_approval_ref': owner_ref})
        allowed = ({o.file_id for o in operations} | {p for o in operations for p in o.before['parents'] + o.target.get('parents', [])}
                   | {m['id'] for o in operations for m in (o.before.get('subtree') or {}).get('members', [])})
        # Transport reads include carried descendants; they are never mutation IDs.
        for children in live['listings'].values():
            for raw in children:
                if raw['id'] in allowed and raw['id'] not in live['raw']:live['raw'][raw['id']] = raw
        auth_ids = {o.file_id for o in operations} | {p for o in operations for p in o.before['parents'] + o.target.get('parents', [])}
        authorize(journal, plan, {s: live['metadata'][s] for s in auth_ids}, instruction_ref, section_id=sid)
        output = {'run_id': run_id, 'plan_sha256': plan.proposal_hash, 'plan_path': save_new(run_dir / 'plan.json', plan.body()),
            'section_path': section, 'section_id': sid, 'selected_source_ids': sorted(pending), 'allowed_ids': sorted(allowed),
            'excluded_ids': sorted(state['excluded']), 'destination_paths': paths, 'semantic_selection': state['semantic_selection'],
            'source_revision': state['capture']['source_revision'], 'source_ids': state['prior_spec']['source_ids'],
            'created_folder_keys': keys, 'source_container_ids': state['layout']['source_container_ids'],
            'instruction_ref': instruction_ref, 'source_metadata_ref': source_ref, 'new_folder_count': len(missing),
            'warnings': [], 'subtree_coverage_version': 1, 'revision_manifest': manifest,
            'revision_manifest_sha256': digest(manifest), 'revision_origin_ref': manifest_ref,
            'prior_destination_paths': state['prior_paths']}
        save_new(run_dir / 'prepared.json', output)
        return output


def validate_revision_authority(journal, plan):
    """Immutable origin checks, plus revocation and expiry on every action gate."""
    from .importer import catalog
    from .section_importer import _not_revoked, _time
    events = [e for e in journal.records() if e['kind'] == 'DESTINATION_REVISION_FROZEN'
              and e['payload'].get('proposal_hash') == plan.proposal_hash]
    if len(events) != 1:raise SafetyError('REVISION_PLAN_ORIGIN_REQUIRED')
    event = events[0]; p = event['payload']; evidence = journal.read_evidence(p['manifest_ref'])
    manifest = evidence['manifest']; owner_ref = evidence['owner_approval_ref']; owner = journal.read_evidence(owner_ref)
    source = catalog(evidence['workbook_export'], expected_workbook_id=manifest['workbook_id'],
                     include_subtree_coverage=True, subtree_section_path=manifest['section_path'])
    read_manifest(manifest, source, manifest['section_path'], manifest['workbook_id'])
    instruction = journal.read_evidence(owner['provenance_ref'])
    if (digest(evidence['plan']) != plan.proposal_hash or plan.proposal_revision != 'revision:' + digest(manifest)
            or p['run_id'] != plan.run_id or owner_ref != p['owner_approval_ref']
            or owner.get('schema_version') != 'uqmc.destination.revision-approval.v1' or owner.get('decision') != 'APPROVED'
            or owner.get('manifest_sha256') != digest(manifest) or owner.get('plan_sha256') != plan.proposal_hash
            or owner.get('section_id') != p['section_id'] or instruction.get('kind') != 'TRUSTED_USER_INSTRUCTION'
            or instruction.get('revision_manifest_sha256') != digest(manifest)
            or set(owner.get('operation_refs', [])) != {o.operation_id for o in plan.operations}):
        raise SafetyError('REVISION_PLAN_AUTHORITY_MISMATCH')
    _not_revoked(journal, owner['approval_id'])
    if owner.get('expires_at_utc') is not None and _time(owner['expires_at_utc']) <= datetime.now(timezone.utc):
        raise SafetyError('SECTION_APPROVAL_EXPIRED')
    items = {x['source_id']: x for x in manifest['items']}
    for op in plan.operations:
        if op.file_id not in items or op.file_id not in evidence['prior_completion_proofs']:
            raise SafetyError('REVISION_OPERATION_OUTSIDE_MANIFEST')
        expected = {'name': items[op.file_id]['target_name']} if op.action == 'RENAME' else {
            'parents': [evidence['destination_paths'][target_parent(items[op.file_id])]]}
        if op.target != expected:raise SafetyError('REVISION_OPERATION_TARGET_MISMATCH')
    return {'schema_version': 'uqmc.section.authority-binding.v1', 'section_import_event_id': event['event_id'],
        'section_import_event_seq': event['seq'], 'section_manifest_ref': p['manifest_ref'], 'section_id': p['section_id'],
        'owner_approval_ref': owner_ref, 'owner_approval_id': owner['approval_id'], 'projection_sha256': digest(manifest),
        'proposal_hash': plan.proposal_hash, 'proposal_revision': plan.proposal_revision, 'expires_at_utc': owner['expires_at_utc']}


def superseded_creation_path_ids(journal, section, registered_paths):
    """Use later verified revision placement, never rewrite creation evidence.

    Ordinary planning already verifies registered destination IDs live. Only
    creation-path hints superseded by a verified revision are omitted; their
    original allocation/marker/authority remains available in the journal.
    """
    origins = [e for e in journal.records() if e['kind'] == 'DESTINATION_REVISION_FROZEN']
    if not origins:return set()
    histories = build_history_index(journal); result = set()
    for event in origins:
        evidence = journal.read_evidence(event['payload']['manifest_ref'])
        manifest = evidence['manifest']; paths = evidence['destination_paths']
        if digest(evidence['plan']) != event['payload']['proposal_hash']:
            raise SafetyError('REVISION_PLAN_ORIGIN_MISMATCH')
        for item in manifest['items']:
            sid = item['source_id']; history = histories.get(sid, {})
            current = history.get('latest_observation', {})
            if (history.get('pending_requests') or current.get('mime_type') != FOLDER
                    or current.get('name') != item['target_name']
                    or current.get('parents') != [paths[target_parent(item)]]):continue
            for old, child_id in evidence.get('prior_destination_paths', {}).items():
                if _inside(old, item['before_path']):
                    new = item['target_path'] + old[len(item['before_path']):]
                    if _inside(new, section) and registered_paths.get(new) == child_id and paths.get(new) == child_id:
                        result.add(child_id)
                elif (old != manifest['section_path'] and _inside(item['before_path'], old)
                        and _inside(old, section) and registered_paths.get(old) == child_id and paths.get(old) == child_id):
                    # The old category container is retained, not recreated or
                    # deleted. Its allocation is no longer a required target.
                    result.add(child_id)
    return result
