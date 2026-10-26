"""Read-only publication of legacy and desired-state execution evidence.

No network, mutation, approval, or journal append. Strict snapshots stay separate
from the explicitly selected destination-section overlay. Folder-creation order
is its sealed authority's folder order; its run ID is the recorded section ID.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import uuid

from .journal import _validate, canonical, digest
from .importer import retired_source_ids
from .shortcut_policy import excluded_shortcut_ids, frozen_shortcut_exclusion_provenance
from .projection import build_projections, validate_snapshot, _ordered_plan_operations

FOLDER = 'application/vnd.google-apps.folder'
DESIRED_STATES = {'DESIRED_PREPARED': 'PREPARED', 'DESIRED_ATTEMPTED': 'ATTEMPTED',
                  'DESIRED_APPLIED': 'VERIFIED_APPLIED', 'DESIRED_ROLLED_BACK': 'VERIFIED_APPLIED'}


class ReadOnlyJournal:
    """Validate a frozen local journal view without creating/chmodding anything."""
    def __init__(self, root, *, checkpoint=None):
        self.root = Path(root)
        with (self.root / '.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self._records = _validate((self.root / 'events.jsonl').read_bytes())
        if checkpoint is not None and len(self._records) != checkpoint:
            raise ValueError('Publication checkpoint does not match journal')
        self._evidence = {}

    @contextmanager
    def locked(self):
        yield

    def records(self):
        return self._records

    def read_evidence(self, ref):
        if not isinstance(ref, str) or not ref.startswith('sha256:') or len(ref) != 71:
            raise ValueError('Invalid evidence reference')
        if ref not in self._evidence:
            raw = (self.root / 'evidence' / (ref[7:] + '.json')).read_bytes()
            value = json.loads(raw)
            if canonical(value) != raw or digest(raw) != ref[7:]:
                raise ValueError('Evidence digest mismatch')
            self._evidence[ref] = value
        return self._evidence[ref]


def _core(value):
    return {'id': value.get('id'), 'name': value.get('name'),
            'parents': sorted(value.get('parents') or []),
            'mime_type': value.get('mimeType', value.get('mime_type'))}


def _blank(source_id, link=None):
    effect = {'effect': 'NOT_TRACKED', 'last_event_state': None, 'operation_ids': [], 'verified_at_utc': None}
    return {'source_id': source_id, 'source_link': link, 'move': copy.deepcopy(effect),
            'rename': copy.deepcopy(effect), 'location': {'effect': 'UNKNOWN', 'ancestor_move_operation_ids': [],
            'verified_path': None, 'path_verified_at_utc': None}, 'observed': {'exact_name': None,
            'parent_ids': None, 'mime_type': None, 'metadata_verified_at_utc': None, 'read_state': 'NEVER_VERIFIED'},
            'reason_codes': ['NO_SYSTEM_RECORD']}


def _observe(item, raw, at):
    core = _core(raw)
    item['observed'] = {'exact_name': core['name'], 'parent_ids': core['parents'],
                        'mime_type': core['mime_type'], 'metadata_verified_at_utc': at, 'read_state': 'VERIFIED'}
    item['reason_codes'] = [x for x in item['reason_codes'] if x not in {'NO_SYSTEM_RECORD', 'REVIEW_REQUIRED'}]


def _plans(journal):
    result = {}
    for event in journal.records():
        p = event['payload']
        if event['kind'] != 'DRAFTED':
            continue
        plan = journal.read_evidence(p['plan_ref'])
        groups = {}
        for order, op in enumerate(_ordered_plan_operations(plan), 1):
            group = groups.setdefault(op['file_id'], {'original': _core(op['before']), 'target': _core(op['before']),
                                                       'operations': {}, 'baseline': op['before']})
            group['target'].update(op['target'])
            group['target']['parents'] = sorted(group['target']['parents'])
            group['operations'][op['operation_id']] = {'action': op['action'], 'order': order}
        result[p['proposal_hash']] = groups
    return result


def _satisfied_preflight_observations(journal, plans, sections, items, latest_legacy):
    """Recover observations omitted from dispatch because their fields were satisfied.

    A drafted target alone is not evidence. Require the approved section import's
    hash-bound raw preflight capture and an unchanged sealed preimage. This is an
    observation at capture time, never a synthetic action or new live readback.
    """
    wanted = {ph for spec in sections for ph in spec.get('plan_sha256s', [spec.get('plan_sha256')])}
    approved = {e['payload'].get('proposal_hash') for e in journal.records() if e['kind'] == 'APPROVED'}
    observations = {}
    for event in journal.records():
        p = event['payload']; ph = p.get('proposal_hash')
        if event['kind'] != 'SECTION_IMPORTED' or ph not in wanted or ph not in approved:
            continue
        candidates = {sid: group for sid, group in plans.get(ph, {}).items()
            if group['original'] == group['target'] and group['target']['mime_type'] != FOLDER
            and sid in items and sid not in latest_legacy
            and items[sid]['observed']['read_state'] == 'NEVER_VERIFIED'
            and all(items[sid][effect]['last_event_state'] is None for effect in ('move', 'rename'))}
        if not candidates:
            continue
        imported = journal.read_evidence(p['manifest_ref'])
        projection = imported['projection']; manifest = imported['manifest']
        # Older non-destination import profiles do not carry this evidence hook.
        reference = projection.get('destination_path_evidence_ref')
        if not reference:
            continue
        owner = journal.read_evidence(imported['owner_approval_ref'])
        if (digest(canonical(imported['plan'])) != ph
                or digest(canonical(projection)) != p['projection_sha256']
                or owner.get('projection_sha256') != p['projection_sha256']
                or imported['owner_approval_ref'] != p['owner_approval_ref']
                or owner.get('decision') != 'APPROVED'
                or manifest.get('section_approval_validated') is not True
                or owner.get('section_id') != projection.get('section_id')):
            raise ValueError('SATISFIED_PREFLIGHT_AUTHORITY_MISMATCH')
        mapping = journal.read_evidence(reference)
        if (mapping.get('section_id') != projection['section_id']
                or mapping.get('input_sha256') != projection.get('input_sha256')):
            raise ValueError('SATISFIED_PREFLIGHT_MAPPING_MISMATCH')
        capture = journal.read_evidence(mapping['provenance_ref'])
        at = manifest['captured_at_utc']
        if datetime.fromisoformat(at.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('SATISFIED_PREFLIGHT_CAPTURE_TIME_REQUIRED')
        members = {m['source_id'] for m in projection['members']} & set(owner['member_source_ids'])
        cached = set(capture.get('history_cached_source_ids', []))
        for sid, group in candidates.items():
            raw = capture.get('metadata', {}).get(sid)
            if (sid not in members or sid in cached or not raw or raw.get('trashed') is not False
                    or group['baseline'].get('trashed') is not False
                    or group['baseline'].get('parents_complete') is not True
                    or _core(raw) != group['original']):
                continue
            observations[sid] = (raw, at)
    return observations


def _desired_state(event):
    if event['kind'] in DESIRED_STATES:
        return DESIRED_STATES[event['kind']]
    p = event['payload']
    if event['kind'] == 'DESIRED_STOPPED':
        return 'UNCERTAIN' if p.get('state') in {'UNCERTAIN', 'UNAVAILABLE'} else 'BLOCKED_CONFLICT'
    if event['kind'] == 'DESIRED_OBSERVED':
        return {'TARGET': 'VERIFIED_APPLIED', 'NOT_APPLIED': 'FAILED_NOT_APPLIED',
                'PARTIAL': 'PREPARED'}.get(p.get('state'))
    return None


def _action(p, group):
    args = p.get('args') or {}
    name = 'name' in args
    move = bool(args.get('addParents') or args.get('removeParents'))
    if not name and not move:
        actions = {group['operations'][x]['action'] for x in p.get('original_operation_ids', []) if x in group['operations']}
        name, move = 'RENAME' in actions, 'MOVE' in actions
    return 'MOVE_AND_RENAME' if name and move else 'RENAME' if name else 'MOVE'


def _publication_exclusions(records):
    """Keep withdrawal/private exclusions stronger than recoverable execution skips.

    A recoverable missing identity is public only through the caller's explicit
    allowlist. This classification does not modify execution exclusion history.
    Any other exclusion for the same ID continues to prohibit public display.
    """
    excluded, recoverable, private = set(), set(), set()
    derived = frozen_shortcut_exclusion_provenance(records)
    for event in records:
        if event['kind'] != 'EXCLUDED':
            continue
        payload = event['payload']
        source = payload.get('file_id')
        excluded.add(source)
        if event['event_id'] in derived:
            continue  # Still execution-excluded, with its earlier typed policy.
        if (payload.get('recoverable') is True and payload.get('effect') == 'NOT_APPLIED'
                and payload.get('reason_code') == 'MISSING_OR_INACCESSIBLE'):
            recoverable.add(source)
        else:
            private.add(source)
    return excluded, recoverable - private, private


def _carried_folder_observations(journal, observations):
    """Rebase an unchanged child's logical path only through verified revision ancestry.

    The child's exact preflight metadata and prior observation must agree. A
    parent move does not invent a child move or a newer child readback timestamp.
    """
    from .destination_publication import revision_folder_updates
    verified = {r['source_id']: r for r in revision_folder_updates(journal) if r['verified']}
    records = journal.records(); latest = {}
    for event in records:
        if _desired_state(event): latest[event['payload'].get('source_id')] = event['seq']
    result = dict(observations)
    # A verified human move changes descendant logical paths, not their IDs,
    # direct parents or historical metadata observation timestamps.
    for revision in verified.values():
        if revision.get('performed_by') != 'user': continue
        prefix = revision['before_path'] + '/'
        for key, old in list(result.items()):
            if old['path'].startswith(prefix):
                result[key] = {**old, 'path': revision['target_path'] + old['path'][len(prefix)-1:]}
    for event in records:
        if event['kind'] != 'DESTINATION_REVISION_FROZEN': continue
        payload = event['payload']; evidence = journal.read_evidence(payload['manifest_ref'])
        raw = journal.read_evidence(evidence['source_metadata_ref'])['metadata']
        prior = {sid:path for path,sid in evidence['prior_destination_paths'].items()}
        target = {sid:path for path,sid in evidence['destination_paths'].items()}
        moved = {i['source_id']:i for i in evidence['manifest']['items']}
        for sid, path in target.items():
            key = (payload['section_id'],sid); old = result.get(key); current = raw.get(sid)
            if sid in moved or old is None or not current or latest.get(sid,0) > event['seq']: continue
            core = _core(current)
            if (current.get('trashed') is not False or core['mime_type'] != FOLDER or
                    old['path'] != prior.get(sid) or
                    core != {'id':sid,'name':old['name'],'parents':sorted(old['parent_ids']),'mime_type':FOLDER}): continue
            ancestor = sid; seen = set(); matched = False
            while ancestor not in seen:
                seen.add(ancestor); metadata = raw.get(ancestor)
                if not metadata or len(metadata.get('parents',[])) != 1: break
                ancestor = metadata['parents'][0]
                if ancestor in moved:
                    move = moved[ancestor]; proof = verified.get(ancestor)
                    matched = bool(proof and proof['target_path'] == move['target_path'] and
                        prior[sid].startswith(move['before_path'] + '/') and
                        path == move['target_path'] + prior[sid][len(move['before_path']):])
                    break
            if matched: result[key] = {**old, 'path':path}
    return result


def retain_reporting_observations(journal, items, proof):
    """Carry an explicitly recorded live reporting correction, never a new action.

    The original published preimage must still match and its journal prefix must
    be intact. Later activity on this identity supersedes the correction. Full
    evidence stays private; normal snapshot redaction still applies below.
    """
    if not proof:return
    records=journal.records();base=proof.get('base_journal_checkpoint')
    if (type(base) is not int or base<1 or base>len(records) or
            records[base-1]['hash']!=proof.get('base_journal_tip_sha256')):
        raise ValueError('REPORTING_RECONCILIATION_JOURNAL_MISMATCH')
    verified=datetime.fromisoformat(proof['verified_at_utc'].replace('Z','+00:00'))
    if verified.tzinfo is None or verified<datetime.fromisoformat(records[base-1]['at'].replace('Z','+00:00')):
        raise ValueError('REPORTING_RECONCILIATION_TIME_INVALID')
    later={e['payload'].get('source_id') or e['payload'].get('file_id') for e in records[base:]}
    seen=set()
    for correction in proof.get('items',[]):
        sid=correction.get('source_id')
        if sid in seen or sid not in items:raise ValueError('REPORTING_RECONCILIATION_ID_MISMATCH')
        seen.add(sid)
        if sid in later:continue
        current=items[sid];prior=correction.get('prior_reporting_observation')
        redacted=copy.deepcopy(current)
        for field in ('exact_name','parent_ids','mime_type'):redacted['observed'][field]=None
        if redacted!=prior:raise ValueError('REPORTING_RECONCILIATION_PREIMAGE_MISMATCH')
        observed=correction.get('observed',{})
        if (observed.get('read_state')!='VERIFIED' or not observed.get('exact_name') or
                not observed.get('parent_ids') or not observed.get('mime_type') or
                observed.get('metadata_verified_at_utc')!=proof.get('verified_at_utc')):
            raise ValueError('REPORTING_RECONCILIATION_OBSERVATION_REQUIRED')
        if (current['observed']['exact_name']!=observed['exact_name'] or
                current['observed']['parent_ids']!=observed['parent_ids'] or
                current['observed']['mime_type']!=observed['mime_type']):
            raise ValueError('REPORTING_RECONCILIATION_TARGET_CHANGED')
        for field in ('move','rename'):
            effect=correction.get(field,{})
            if (effect.get('effect')!='APPLIED' or effect.get('last_event_state')!='VERIFIED_APPLIED' or
                    effect.get('operation_ids')!=current[field]['operation_ids'] or
                    effect.get('verified_at_utc')!=proof['verified_at_utc']):
                raise ValueError('REPORTING_RECONCILIATION_EFFECT_MISMATCH')
        # Every corrected effect must have a historical successful readback.
        applied=[e for e in records[:base] if e['kind']=='DESIRED_APPLIED' and
                 e['payload'].get('source_id')==sid and e['payload'].get('observed_ref')]
        if not applied:raise ValueError('REPORTING_RECONCILIATION_COMPLETION_REQUIRED')
        successful=_core(journal.read_evidence(applied[-1]['payload']['observed_ref']))
        if successful!={'id':sid,'name':observed['exact_name'],'parents':sorted(observed['parent_ids']),
                        'mime_type':observed['mime_type']}:
            raise ValueError('REPORTING_RECONCILIATION_TARGET_CHANGED')
        items[sid]={key:copy.deepcopy(correction[key]) for key in current}


def build_publication(journal, *, allowed_source_ids, identity_basis, sections=(), verified_source_links=None, reporting_corrections=None):
    """Return workbook/html strict snapshots, private operation_log, and sections.

    sections entries: section_id, title, proposal_path, plan_sha256, source_ids
    (files only), destination_id, destination_mode (REUSED_MOVED or CREATED),
    optional destination_ids (created subtree), source_folder_ids_left_in_place.
    Completion is checked against sealed targets and latest verified observations;
    this exports historical readback, not a new live verification.
    """
    records = journal.records()
    plans = _plans(journal)
    out = build_projections(journal, allowed_source_ids=allowed_source_ids, identity_basis=identity_basis,
                            verified_source_links=verified_source_links)
    workbook, log = out['workbook'], out['operation_log']
    items = {x['source_id']: x for x in workbook['items']}
    ops = {(x['run_id'], x['operation_id']): x for x in workbook['operations']}
    latest_legacy = {}
    for e in records:
        if e['kind'] in {'PREPARED','ATTEMPTED','VERIFIED_APPLIED','FAILED_NOT_APPLIED','UNCERTAIN','BLOCKED_CONFLICT','SKIPPED','RECONCILED_AT_TARGET'}:
            latest_legacy[e['payload'].get('file_id')] = e['seq']
    observed_raw, applied_folder_moves, created, inherited = {}, {}, {}, {}
    latest_desired = {}
    histories = {}
    for e in records:
        p, state = e['payload'], _desired_state(e)
        if not state or not p.get('source_id'):
            continue
        source = p['source_id']
        latest_desired[source] = e['seq']
        group = plans.get(p.get('plan_sha256'), {}).get(source)
        if group is None:
            raise ValueError('Desired event has no sealed plan item')
        item = items.setdefault(source, _blank(source, (verified_source_links or {}).get(source)))
        key = (p['run_id'], e['operation_id'])
        history = histories.setdefault(key, {})
        history.update(p)
        h = history
        action = _action(h, group)
        order = p.get('operation_order')
        if type(order) is not int or order < 1:
            raise ValueError('Desired event has no recorded compiled item order')
        deps = ['desired:' + p['plan_sha256'][:12] + ':' + d + ':' + p['direction'] for d in p.get('dependencies', [])]
        observed = journal.read_evidence(p['observed_ref']) if p.get('observed_ref') else None
        at = p.get('metadata_verified_at_utc', e['at'])
        op = ops.setdefault(key, {'operation_id': e['operation_id'], 'run_id': p['run_id'], 'source_id': source,
            'action': action, 'direction': p['direction'], 'operation_order': order, 'depends_on_operation_ids': deps,
            'approval_state': 'NOT_APPROVED', 'approved_plan_sha256': p['plan_sha256'],
            'related_forward_operation_id': None, 'verified_at_utc': None, 'reason_code': None})
        op.update(state=state, action=action)
        if state == 'VERIFIED_APPLIED':
            op['verified_at_utc'] = at
        op['reason_code'] = ('OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN' if e['kind']=='DESIRED_OBSERVED' and state=='VERIFIED_APPLIED' else
                             'REVIEW_REQUIRED' if state in {'UNCERTAIN','BLOCKED_CONFLICT'} else None)
        before = journal.read_evidence(h['before_ref']) if h.get('before_ref') else group['original']
        target = journal.read_evidence(h['target_ref']) if h.get('target_ref') else group['target']
        if p.get('forward_event_id'):
            forward = next((x for x in records if x['event_id'] == p['forward_event_id']), None)
            op['related_forward_operation_id'] = forward['operation_id'] if forward else None
        log['rows'].append([e['event_id'], e['at'], p['run_id'], e['operation_id'], e['attempt'], order,
            json.dumps(deps), p['direction'], action, json.dumps(p.get('original_operation_ids', [])), source,
            _core(before)['mime_type'], before.get('name'), json.dumps(before.get('parents')), target.get('name'),
            json.dumps(target.get('parents')), state, observed.get('name') if observed else None,
            json.dumps(observed.get('parents')) if observed else None, h.get('before_ref'), p.get('observed_ref'),
            h.get('before_ref') if h.get('audit_permissions') else None, op['related_forward_operation_id'],
            'explicit local CLI', p.get('request_outcome', op['reason_code']), e['hash']])
        if e['seq'] <= latest_legacy.get(source, 0):
            continue
        for label, field in [('rename','name'), ('move','parents')]:
            effect = item[label]
            changed = group['original'][field] != group['target'][field]
            actual = (action in {'MOVE_AND_RENAME',label.upper()})
            if actual and e['operation_id'] not in effect['operation_ids']:
                effect['operation_ids'].append(e['operation_id'])
            if observed and state in {'VERIFIED_APPLIED','FAILED_NOT_APPLIED','PREPARED'}:
                value = _core(observed)[field]
                if changed and value == group['target'][field]:
                    effect.update(effect='APPLIED', last_event_state='VERIFIED_APPLIED', verified_at_utc=at)
                elif changed and value == group['original'][field]:
                    effect.update(effect='REVERTED' if p['direction']=='ROLLBACK' else 'UNCHANGED', last_event_state=state, verified_at_utc=at)
                elif not changed:
                    # No direct parent change is distinct from relocation with an ancestor.
                    if effect['effect'] == 'NOT_TRACKED':
                        effect.update(effect='UNCHANGED', last_event_state=None, verified_at_utc=at)
                else:
                    effect.update(effect='CONFLICT', last_event_state='BLOCKED_CONFLICT')
            elif actual:
                effect['last_event_state'] = state
                if state in {'ATTEMPTED','UNCERTAIN'}:
                    effect['effect'] = 'UNKNOWN'
                elif state == 'BLOCKED_CONFLICT':
                    effect['effect'] = 'CONFLICT'
                elif state == 'PREPARED' and effect['effect'] == 'NOT_TRACKED':
                    effect['effect'] = 'UNCHANGED'
        if observed:
            _observe(item, observed, at)
            if state == 'VERIFIED_APPLIED' and _core(observed) == (
                    _core(target) if p['direction'] == 'ROLLBACK' else group['target']):
                # Current verified state supersedes an older unresolved request.
                # Attribution/terminal uncertainty remains in its operation and log.
                item['reason_codes'] = [code for code in item['reason_codes'] if code not in {
                    'OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN', 'REQUEST_NOT_PROVEN_TERMINAL'}]
            observed_raw[source] = _core(observed)
            if state == 'VERIFIED_APPLIED' and group['target']['mime_type'] == FOLDER and group['original']['parents'] != group['target']['parents']:
                if _core(observed)['parents'] == group['target']['parents']:
                    applied_folder_moves[source] = e['operation_id']
                    for child, g in plans[p['plan_sha256']].items():
                        if source in g['original']['parents'] and source in g['target']['parents']:
                            inherited.setdefault(child, set()).add(e['operation_id'])
                elif source in applied_folder_moves:
                    applied_folder_moves.pop(source)
        elif state in {'ATTEMPTED','UNCERTAIN','BLOCKED_CONFLICT'}:
            item['observed']['read_state'] = 'CONFLICT' if state=='BLOCKED_CONFLICT' else 'UNAVAILABLE'
    # Approval describes current authority, never proves execution. No credentials are read.
    approval_by_hash = {}
    for e in records:
        p = e['payload']
        if e['kind']=='APPROVED':
            approval_by_hash[p.get('proposal_hash')] = ('APPROVED', p.get('approval_id'))
        if e['kind'] in {'REVOKED','SECTION_APPROVAL_REVOKED','SECTION_APPROVAL_SUPERSEDED'}:
            for ph, (_, aid) in list(approval_by_hash.items()):
                if aid == p.get('approval_id'):
                    approval_by_hash[ph] = ('WITHDRAWN', aid)
    for op in ops.values():
        if op['operation_id'].startswith('desired:'):
            op['approval_state'] = approval_by_hash.get(op['approved_plan_sha256'], ('NOT_APPROVED',None))[0]
    # Creation is verified from its readback and sealed key -> stable ID authority.
    creation_states={'REQUIRED_FOLDER_PREPARED':'PREPARED','REQUIRED_FOLDER_ATTEMPTED':'ATTEMPTED',
                     'REQUIRED_FOLDER_VERIFIED':'VERIFIED_APPLIED','REQUIRED_FOLDER_UNCERTAIN':'UNCERTAIN'}
    for e in records:
        if e['kind'] not in creation_states:
            continue
        p=e['payload']; authority=journal.read_evidence(p['authority_ref'])
        matches=[i for i,x in enumerate(authority['folders'],1) if x['key']==p['key'] and x['name']==p['name']]
        if len(matches)!=1:
            raise ValueError('Creation authority mapping mismatch')
        order=matches[0]; state=creation_states[e['kind']]
        raw=journal.read_evidence(p['observed_ref']) if p.get('observed_ref') else None
        if raw and (_core(raw) != {'id':p['file_id'],'name':p['name'],'parents':[p['parent_id']],'mime_type':FOLDER}):
            raise ValueError('Created folder readback mismatch')
        key=(p['section_id'],e['operation_id'])
        old=ops.get(key,{})
        ops[key]={'operation_id':e['operation_id'],'run_id':p['section_id'],'source_id':p['file_id'],
            'action':'CREATE_FOLDER','direction':'FORWARD','state':state,'operation_order':order,
            'depends_on_operation_ids':[],'approval_state':'APPROVED' if authority.get('decision')=='APPROVED' else 'NOT_APPROVED',
            'approved_plan_sha256':None,'related_forward_operation_id':None,
            'verified_at_utc':e['at'] if raw else old.get('verified_at_utc'),'reason_code':None}
        log['rows'].append([e['event_id'],e['at'],p['section_id'],e['operation_id'],e['attempt'],order,'[]','FORWARD',
            'CREATE_FOLDER',json.dumps([p['key']]),p['file_id'],FOLDER,None,None,p['name'],json.dumps([p['parent_id']]),
            state,raw.get('name') if raw else None,json.dumps(raw.get('parents')) if raw else None,
            p.get('parent_before_ref'),p.get('observed_ref'),None,None,'explicit local CLI',None,e['hash']])
        if raw:
            created[p['file_id']]={'id':p['file_id'],'name':p['name'],'parent_ids':[p['parent_id']],
                'verified_at_utc':e['at'],'destination_link':'https://drive.google.com/drive/folders/'+p['file_id']}
            item=items.setdefault(p['file_id'],_blank(p['file_id']))
            if latest_desired.get(p['file_id'], 0) <= e['seq']:
                _observe(item,raw,e['at']); observed_raw[p['file_id']]=_core(raw)
            else:
                # Creation history stays intact; a later rename/move readback is
                # the destination's current observed state, including uncertainty.
                observed = item['observed']
                if observed['read_state'] == 'VERIFIED':
                    created[p['file_id']].update(name=observed['exact_name'], parent_ids=observed['parent_ids'],
                        verified_at_utc=observed['metadata_verified_at_utc'])
                else:
                    created.pop(p['file_id'], None)
    for source, (raw, at) in _satisfied_preflight_observations(
            journal, plans, sections, items, latest_legacy).items():
        item = items[source]
        _observe(item, raw, at)
        for label in ('move', 'rename'):
            item[label].update(effect='UNCHANGED', verified_at_utc=at)
        observed_raw[source] = _core(raw)
    retain_reporting_observations(journal, items, reporting_corrections)
    for source,item in items.items():
        ancestors=sorted(set(item['location']['ancestor_move_operation_ids']) | inherited.get(source,set()))
        item['location']['ancestor_move_operation_ids']=ancestors
        if item['move']['effect'] in {'UNKNOWN','CONFLICT'}:
            item['location']['effect']='UNKNOWN'
        elif item['move']['effect']=='APPLIED':
            item['location']['effect']='DIRECT_AND_INHERITED' if ancestors else 'DIRECT'
        elif ancestors:
            item['location']['effect']='INHERITED'
        elif item['observed']['read_state']=='VERIFIED':
            item['location']['effect']='NONE'
    excluded, recoverable_excluded, private_excluded = _publication_exclusions(records)
    retired = retired_source_ids(records)
    policy_excluded = excluded_shortcut_ids(records)
    from .revision_cleanup import retired_parents
    retired_folders = retired_parents(journal)
    derived_policy_ids = {p['file_id'] for p in frozen_shortcut_exclusion_provenance(records).values()}
    for source in policy_excluded:
        if source in items:
            if source in derived_policy_ids and source not in private_excluded:
                items[source]['reason_codes'] = [code for code in items[source]['reason_codes'] if code != 'WITHDRAWN']
            items[source]['reason_codes'] = list(dict.fromkeys([
                *items[source]['reason_codes'], 'POLICY_EXCLUDED', 'USER_EXCLUDED_SHORTCUT']))
    for source in retired_folders:
        if source in items:
            items[source]['reason_codes'] = list(dict.fromkeys([
                *items[source]['reason_codes'], 'RETIRED_EMPTY_FOLDER', 'VERIFIED_TRASHED']))
    section_rows=[]; allowed=set(allowed_source_ids)-private_excluded
    for source in recoverable_excluded:
        item = items.get(source)
        if item is not None:
            # Preserve observed effects, but distinguish a recoverable execution
            # skip from permanent withdrawal. Missing is not verified unchanged.
            item['reason_codes'] = [code for code in item['reason_codes'] if code != 'WITHDRAWN']
            item['reason_codes'] = list(dict.fromkeys([
                *item['reason_codes'], 'EXCLUDED', 'MISSING_OR_INACCESSIBLE', 'NOT_APPLIED']))
    for source in retired:
        item = items.get(source)
        if item is not None:
            # Retirement is an audit disposition, never an observed Drive effect.
            item['reason_codes'] = list(dict.fromkeys([
                *item['reason_codes'], 'RETIRED', 'USER_CONFIRMED_DELETED', 'NOT_APPLIED']))
    destination_observations={}
    for event in records:
        if event['kind']!='DESTINATION_VERIFIED':continue
        p=event['payload'];raw=journal.read_evidence(p['observed_ref'])
        core=_core(raw)
        if (core['id']!=p['file_id'] or core['mime_type']!=FOLDER or
            core['name']!=p['exact_name'] or core['parents']!=sorted(p['parent_ids']) or
            raw.get('trashed') is True):
            raise ValueError('Destination observation mismatch')
        destination_observations[(p['section_id'],p['file_id'])]={
            'id':core['id'],'name':core['name'],'parent_ids':core['parents'],
            'verified_at_utc':event['at'],'mode':'REUSED_EXISTING',
            'destination_link':'https://drive.google.com/drive/folders/'+core['id'],
            'path':p['proposal_path']}
    destination_observations = _carried_folder_observations(journal, destination_observations)
    for spec in sections:
        ids=spec['source_ids']; dest=spec['destination_id']
        hashes=spec.get('plan_sha256s',[spec.get('plan_sha256')])
        if not hashes or len(hashes)!=len(set(hashes)) or any(h not in plans for h in hashes):
            raise ValueError('Section plan history missing or ambiguous')
        # Journal insertion order, never caller ordering, decides the latest
        # sealed target for an item present in several continuation batches.
        plan={}
        for ph,part in plans.items():
            if ph in hashes:plan.update(part)
        if len(ids)!=len(set(ids)) or not set(ids)<=allowed or not set(spec.get('source_folder_ids_left_in_place',[]))<=allowed:
            raise ValueError('SECTION_IDENTITIES_NOT_PUBLIC_ALLOWLISTED')
        retired_ids = sorted(set(ids) & retired)
        policy_ids = sorted(set(ids) & policy_excluded)
        ids = [sid for sid in ids if sid not in retired | policy_excluded]
        if spec['destination_mode'] not in {'REUSED_MOVED','REUSED_EXISTING','CREATED'}:
            raise ValueError('Unknown destination mode')
        def matching_destination_observation(sid):
            observed = destination_observations.get((spec['section_id'], sid))
            expected_path = spec.get('destination_paths', {}).get(
                sid, spec['proposal_path'] if sid == dest else None)
            if observed is None or observed['path'] != expected_path:
                return None
            if sid in plan:
                core = {'id': sid, 'name': observed['name'],
                        'parents': observed['parent_ids'], 'mime_type': FOLDER}
                # A no-op folder has no action result. Its explicit destination
                # verification can prove the exact target, but cannot replace a
                # later failed/conflicting action observation or a different plan.
                if (core != plan[sid]['target'] or
                        items[sid]['observed']['read_state'] != 'NEVER_VERIFIED'):
                    return None
            return dict(observed)

        observed_dest=destination_observations.get((spec['section_id'],dest))
        if dest not in allowed and dest not in created and observed_dest is None:
            raise ValueError('Destination is not an allowed source or verified creation')
        verified=[s for s in ids if s not in excluded and s in plan and items[s]['observed']['read_state']=='VERIFIED' and
                  {'id':s,'name':items[s]['observed']['exact_name'],'parents':items[s]['observed']['parent_ids'],
                   'mime_type':items[s]['observed']['mime_type']}==plan[s]['target']]
        destination_ids=spec.get('destination_ids',[dest]); required_created=[s for s in destination_ids
            if s not in allowed and (spec['section_id'],s) not in destination_observations]
        root_ok=(dest in created if spec['destination_mode']=='CREATED' else
                 (dest in plan and items[dest]['observed']['read_state']=='VERIFIED' and
                 items[dest]['observed']['parent_ids']==plan[dest]['target']['parents'] and
                 items[dest]['observed']['exact_name']==plan[dest]['target']['name']) or
                 matching_destination_observation(dest) is not None)
        verified_destinations=[]
        for sid in destination_ids:
            if sid in created:
                d={**created[sid],'mode':'CREATED'}
            elif sid in plan and sid in items:
                item=items[sid];obs=item['observed']
                if obs['read_state']=='VERIFIED' and {'id':sid,'name':obs['exact_name'],
                    'parents':obs['parent_ids'],'mime_type':obs['mime_type']}==plan[sid]['target']:
                    d={'id':sid,'name':obs['exact_name'],'parent_ids':obs['parent_ids'],
                       'verified_at_utc':obs['metadata_verified_at_utc'],'mode':'REUSED_MOVED',
                       'destination_link':'https://drive.google.com/drive/folders/'+sid}
                else:
                    d=matching_destination_observation(sid)
                    if d is None:continue
            else:
                d=matching_destination_observation(sid)
                if d is None:continue
            if sid in spec.get('destination_paths',{}):d['path']=spec['destination_paths'][sid]
            verified_destinations.append(d)
        # A created destination can have a later approved revision. Its original
        # creation/current link does not prove that new name/parent target applied.
        destination_targets_satisfied = all(sid not in plan or
            (items[sid]['observed']['read_state']=='VERIFIED' and
             {'id':sid, 'name':items[sid]['observed']['exact_name'],
              'parents':items[sid]['observed']['parent_ids'],
              'mime_type':items[sid]['observed']['mime_type']}==plan[sid]['target']) or
            matching_destination_observation(sid) is not None for sid in destination_ids)
        # A file at its own target is not complete while its containing
        # folder awaits an approved rename/move. Require a satisfied chain
        # back to this section root; keep historical effects in the snapshot.
        reachable = {dest} if root_ok else set()
        pending_destinations = list(verified_destinations)
        while pending_destinations:
            advanced = []
            for d in pending_destinations:
                sid = d['id']
                target_ok = (sid not in plan or
                    (items[sid]['observed']['read_state'] == 'VERIFIED' and
                     {'id': sid, 'name': items[sid]['observed']['exact_name'],
                      'parents': items[sid]['observed']['parent_ids'],
                      'mime_type': items[sid]['observed']['mime_type']} == plan[sid]['target']) or
                    matching_destination_observation(sid) is not None)
                if target_ok and (sid in reachable or
                        (d.get('parent_ids') and set(d['parent_ids']) <= reachable)):
                    reachable.add(sid)
                    advanced.append(d)
            if not advanced:
                break
            for d in advanced:
                pending_destinations.remove(d)
        verified = [sid for sid in verified if items[sid]['observed']['parent_ids']
                    and set(items[sid]['observed']['parent_ids']) <= reachable]
        complete=(len(verified)==len(ids) and root_ok and all(s in created for s in required_created)
                  and destination_targets_satisfied
                  and set(destination_ids)<={d['id'] for d in verified_destinations})
        times=[items[s]['observed']['metadata_verified_at_utc'] for s in verified]
        section_rows.append({'section_id':spec['section_id'],'title':spec['title'],'path':spec['proposal_path'],
            'status':'COMPLETED' if complete else 'PARTIAL' if verified else 'NOT_STARTED',
            'destination_id':dest,'destination_url':'https://drive.google.com/drive/folders/'+dest,
            'mode':spec['destination_mode'],'expected_file_count':len(ids),'completed_file_count':len(verified),
            'remaining_file_count':len(ids)-len(verified),'verified_at_utc':max(times) if times else None,
            'verified_file_ids':verified,'verified_destinations':verified_destinations,
            'pending_destination_ids':[sid for sid in destination_ids if sid not in reachable],
            'retired_file_ids':retired_ids,'retired_file_count':len(retired_ids),
            'policy_excluded_file_ids':policy_ids,'policy_excluded_file_count':len(policy_ids),
            'created_destinations':[{**created[s], **({'path':spec['destination_paths'][s]} if s in spec.get('destination_paths',{}) else {})} for s in destination_ids if s in created],
            'retained_source_folder_ids':spec.get('source_folder_ids_left_in_place',[]),
            'created_folder_ids':[s for s in destination_ids if s in created],
            'created_folder_count':sum(s in created for s in destination_ids),
            'source_folder_status_basis':'NOT_MOVED_BY_THIS_RUN',
            'rename_effects':sum(items[s]['rename']['effect']=='APPLIED' for s in ids),
            'direct_move_effects':sum(items[s]['move']['effect']=='APPLIED' for s in ids),
            'inherited_locations':sum(items[s]['location']['effect']=='INHERITED' for s in ids)})
    # Created destination identities are outside the original source-inventory basis.
    # Keep them in the explicit destination overlay and private log only.
    basis_ids=set(identity_basis['source_ids'])
    workbook['items']=[item for source,item in items.items() if source in basis_ids]
    workbook['operations']=[op for op in ops.values() if op['source_id'] in basis_ids]
    times=[i['observed']['metadata_verified_at_utc'] for i in items.values() if i['observed']['metadata_verified_at_utc']]
    workbook['coverage']['last_reconciled_at_utc']=max(times) if times else None
    html=copy.deepcopy(workbook)
    html['items']=[i for i in html['items'] if i['source_id'] in allowed]
    html['operations']=[o for o in html['operations'] if o['source_id'] in allowed]
    public_ops={o['operation_id'] for o in html['operations']}
    for op in html['operations']:
        op['depends_on_operation_ids']=[x for x in op['depends_on_operation_ids'] if x in public_ops]
        if op['related_forward_operation_id'] not in public_ops: op['related_forward_operation_id']=None
    for item in html['items']:
        for field in ('exact_name','parent_ids','mime_type'): item['observed'][field]=None
        item['location']['verified_path']=None
        item['location']['ancestor_move_operation_ids']=[x for x in item['location']['ancestor_move_operation_ids'] if x in public_ops]
    event_order={e['event_id']:e['seq'] for e in records}
    log['rows'].sort(key=lambda r:event_order[r[0]])
    overlay={'schema_version':'uqmc.execution.sections.v1','snapshot_id':None,'journal_sequence':workbook['journal_sequence'],
             'journal_tip_sha256':workbook['journal_tip_sha256'],'generated_at_utc':workbook['generated_at_utc'],
             'basis':'verified_journal_readback','other_sections':'NOT_TRACKED','sections':section_rows,
             'retired_empty_folders':[{'source_id':sid, 'verified_at_utc':proof['at'],
                 'reason_code':'VERIFIED_EMPTY_PARENT_TRASHED'} for sid,proof in sorted(retired_folders.items())
                 if sid in allowed or sid in created]}
    stable=lambda x:{k:v for k,v in x.items() if k not in {'snapshot_id','generated_at_utc'}}
    snapshot_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'uqmc.publication.v1:'+digest(canonical([stable(workbook),stable(html),stable(log),stable(overlay)]))))
    for obj in (workbook,html,log,overlay):obj['snapshot_id']=snapshot_id
    validate_snapshot(workbook);validate_snapshot(html)
    out.update(workbook=workbook,html=html,operation_log=log,sections=overlay)
    out['diagnostics'].update(desired_event_count=sum(e['kind']=='DESIRED_APPLIED' for e in records),
                              verified_created_folders=len(created),publication_writes=0)
    return out


def main(argv=None):
    """Freeze derived files only; no publishing and no source or journal writes."""
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',required=True)
    parser.add_argument('--checkpoint',type=int,required=True)
    parser.add_argument('--identity-basis',required=True)
    parser.add_argument('--sections',required=True,help='Private JSON list using build_publication section inputs')
    parser.add_argument('--output-directory',required=True)
    args=parser.parse_args(argv)
    basis=json.loads(Path(args.identity_basis).read_text())
    specs=json.loads(Path(args.sections).read_text())
    journal=ReadOnlyJournal(args.journal,checkpoint=args.checkpoint)
    bundle=build_publication(journal,allowed_source_ids=basis['source_ids'],identity_basis=basis,sections=specs)
    directory=Path(args.output_directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=directory/(bundle['workbook']['snapshot_id']+'-publication-bundle.json')
    data=canonical(bundle)
    if target.exists():
        prior=json.loads(target.read_bytes())
        # Generation timestamps may differ, but a snapshot ID binds all meaningful content.
        if prior['workbook']['snapshot_id']!=bundle['workbook']['snapshot_id']:
            raise ValueError('Existing publication artifact mismatch')
    else:
        import os
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
    print(json.dumps({'bundle':str(target),'snapshot_id':bundle['workbook']['snapshot_id'],
        'journal_sequence':bundle['workbook']['journal_sequence'],'operation_log_rows':len(bundle['operation_log']['rows']),
        'sections':[{'title':s['title'],'status':s['status'],'completed_file_count':s['completed_file_count'],
                     'expected_file_count':s['expected_file_count']} for s in bundle['sections']['sections']],
        'published':False}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
