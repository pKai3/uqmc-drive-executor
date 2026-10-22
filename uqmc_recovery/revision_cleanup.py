"""Recoverable cleanup of empty old parents proved obsolete by one completed run.

No broad search, permanent deletion, sharing changes or automatic retries. The
ordinary destination command calls this only after a successful unchanged-sheet
run. The explicit CLI can recover a named historical revision. Trash remains
subject to Google's retention policy; rollback first needs its parents restored.
"""
from __future__ import annotations
import argparse
import copy
import json
from collections import Counter
from pathlib import Path
import uuid

from .destination_history import build_history_index, _core
from .direct_transport import GoogleDriveTransport, TransportError
from .journal import Journal
from .importer import retired_source_ids, RETIRED_MARKER
from .model import FOLDER, SafetyError, digest

PREFIX = 'REVISION_PARENT_TRASH_'
REASON = 'EMPTY_OBSOLETE_REVISION_PARENT'


def retired_parents(journal):
    result = {}
    for event in journal.records():
        if event['kind'] == PREFIX+'RESTORE_VERIFIED':
            p=event['payload'];prior=result.get(p['source_id']);observed=journal.read_evidence(p['observed_ref'])
            if (not prior or p.get('retirement_event_id')!=prior['event_id'] or observed.get('trashed') is not False
                    or _core(observed)!=_core(journal.read_evidence(prior['before_ref']))):
                raise SafetyError('INVALID_EMPTY_PARENT_RESTORE_EVIDENCE')
            del result[p['source_id']];continue
        if event['kind'] != PREFIX + 'VERIFIED':continue
        p=event['payload'];before=journal.read_evidence(p['before_ref']);after=journal.read_evidence(p['observed_ref'])
        binding=journal.read_evidence(p['revision_manifest_ref'])
        if binding.get('run_id',binding.get('plan',{}).get('run_id'))!=p.get('run_id'):
            raise SafetyError('INVALID_EMPTY_PARENT_RETIREMENT_BINDING')
        if (p.get('reason') != REASON or not p.get('run_id') or not p.get('revision_manifest_ref')
                or _core(before) != _core(after) or before.get('trashed') is not False
                or after.get('trashed') is not True or after.get('id') != p.get('source_id')
                or after.get('mimeType',after.get('mime_type')) != FOLDER
                or p.get('name') != after['name'] or p.get('parent_ids') != after['parents']):
            raise SafetyError('INVALID_EMPTY_PARENT_RETIREMENT_EVIDENCE')
        result[p['source_id']]={**copy.deepcopy(p),'at':event['at'],'sequence':event['seq'],'event_id':event['event_id']}
    return result


def reconcile_restored(journal,factory,source_ids):
    """Observe user-restored parents; never perform an untrash operation."""
    retired=retired_parents(journal);ids=set(source_ids)&set(retired)
    if not ids:return []
    transport=factory(ids,set(),set(),False);restored=[]
    with journal.locked():
        for sid in sorted(ids):
            p=retired[sid];raw=getattr(transport,'get_trash_candidate',transport.get_file)(sid)
            if raw.get('trashed') is False and _core(raw)==_core(journal.read_evidence(p['before_ref'])):
                journal.append(PREFIX+'RESTORE_VERIFIED',payload={'source_id':sid,'run_id':p['run_id'],
                    'retirement_event_id':p['event_id'],'observed_ref':journal.evidence(raw),
                    'prior_path':p['prior_path'],'name':p['name'],'parent_ids':p['parent_ids']})
                restored.append(sid)
    return restored


def retired_parent_ids(journal):return set(retired_parents(journal))


def filter_registry(journal,registry):
    """Retire active lookups only; original runs and immutable events remain."""
    value=copy.deepcopy(registry);retired=retired_parent_ids(journal)
    if not retired:return value
    for entry in value.get('sections',[]):
        entry['destination_paths']={p:s for p,s in entry.get('destination_paths',{}).items() if s not in retired}
        spec=entry.get('publication_spec',entry)
        if spec.get('destination_id') in retired:raise SafetyError('RETIRED_SECTION_ROOT_CONFLICT')
        spec['destination_paths']={s:p for s,p in spec.get('destination_paths',{}).items() if s not in retired}
        for key in ('destination_ids','created_folder_ids','retained_source_folder_ids','source_folder_ids_left_in_place'):
            if key in spec:spec[key]=[s for s in spec[key] if s not in retired]
    return value


def _assigned_ids(workbook,retired=()):
    if not isinstance(workbook,dict) or not isinstance(workbook.get('sheets'),dict):
        raise SafetyError('CLEANUP_FROZEN_WORKBOOK_REQUIRED')
    result=set()
    for rows in workbook['sheets'].values():
        if not rows or 'Source ID' not in rows[0]:continue
        pos=rows[0].index('Source ID')
        result.update(row[pos] for row in rows[1:] if len(row)>pos and isinstance(row[pos],str) and row[pos])
    return result-set(retired)


def candidates(journal,prepared,registry,workbook,protected_ids=()):
    """Only removed direct parents with captured identity and completed effects."""
    run_id=prepared['run_id'];records=journal.records()
    drafted=[e for e in records if e['kind']=='DRAFTED' and e['payload'].get('proposal_hash')==prepared['plan_sha256']]
    if len(drafted)!=1:raise SafetyError('CLEANUP_EXACT_FROZEN_PLAN_REQUIRED')
    plan=journal.read_evidence(drafted[0]['payload']['plan_ref'])
    if digest(plan)!=prepared['plan_sha256'] or plan.get('run_id')!=run_id:
        raise SafetyError('CLEANUP_PLAN_BINDING_MISMATCH')
    history=build_history_index(journal);targets={};old_parents=set()
    for op in plan['operations']:
        sid=op['file_id'];target=targets.setdefault(sid,copy.deepcopy(op['before']));target.update(op['target'])
        if op['action']=='MOVE':old_parents.update(set(op['before']['parents'])-set(op['target']['parents']))
    for sid,target in targets.items():
        h=history.get(sid,{})
        if (h.get('pending_requests') or h.get('rolled_back_fields') or not h.get('latest_observation')
                or _core(h['latest_observation'])!=_core(target)):
            raise SafetyError('CLEANUP_RUN_NOT_VERIFIED_COMPLETE:'+sid)
    metadata=journal.read_evidence(prepared['source_metadata_ref'])['metadata']
    paths=prepared.get('prior_destination_paths',prepared.get('destination_paths',{}))
    retired=retired_source_ids(records)
    for rows in workbook.get('sheets',{}).values():
        if not rows or 'Source ID' not in rows[0] or 'Evidence / rationale' not in rows[0]:continue
        sidpos=rows[0].index('Source ID');whypos=rows[0].index('Evidence / rationale')
        for row in rows[1:]:
            if len(row)>max(sidpos,whypos) and isinstance(row[whypos],str) and row[whypos].splitlines()[:1]==[RETIRED_MARKER]:
                retired.add(row[sidpos])
    protected=set(protected_ids)|set(prepared.get('excluded_ids',[]))|set(targets)|_assigned_ids(workbook,retired)
    protected.update(p for target in targets.values() for p in target['parents'])
    # Protect fresh assigned destinations (including empty future destinations).
    # Prefix matching is conservative: literal slashes can over-protect, never
    # authorize an additional deletion or invent a hierarchy.
    fresh_paths=set()
    for rows in workbook['sheets'].values():
        if not rows:continue
        positions=[n for n,key in enumerate(rows[0]) if key in
                   {'Proposed destination folder','Exact target path','Proposed target path'}]
        sidpos=rows[0].index('Source ID') if 'Source ID' in rows[0] else None
        for row in rows[1:]:
            if sidpos is not None and len(row)>sidpos and row[sidpos] in retired:continue
            fresh_paths.update(row[n] for n in positions if len(row)>n and isinstance(row[n],str) and row[n])
    for path,sid in prepared.get('destination_paths',{}).items():
        if any(value==path or value.startswith(path+'/') for value in fresh_paths):protected.add(sid)

    for entry in registry.get('sections',[]):
        spec=entry.get('publication_spec',entry)
        protected.add(spec.get('destination_id'))
        if spec.get('section_id')!=prepared['section_id']:
            protected.update(entry.get('destination_paths',{}).values())
    output=[]
    for sid in sorted(old_parents):
        if sid in protected:continue
        raw=metadata.get(sid)
        if not raw or raw.get('mimeType',raw.get('mime_type'))!=FOLDER or raw.get('trashed') is not False:
            continue  # Unknown ancestor/provenance never broadens cleanup scope.
        matching=[path for path,value in paths.items() if value==sid]
        if len(matching)>1:raise SafetyError('CLEANUP_PARENT_PATH_AMBIGUOUS:'+sid)
        output.append({'source_id':sid,'before':raw,'prior_path':matching[0] if matching else raw['name']})
    # Candidate parents can themselves be nested. Never discover more ancestors.
    def depth(item):
        count=0;seen=set();sid=item['source_id']
        while sid in metadata and metadata[sid].get('parents'):
            if sid in seen:raise SafetyError('CLEANUP_PARENT_CYCLE')
            seen.add(sid);sid=metadata[sid]['parents'][0];count+=1
        return count
    return sorted(output,key=depth,reverse=True)


def cleanup_run(journal,prepared,registry,workbook,factory,*,apply=False,protected_ids=()):
    selected=candidates(journal,prepared,registry,workbook,protected_ids)
    if not selected:return {'counts':{},'items':[],'candidate_count':0}
    ids={i['source_id'] for i in selected};transport=factory(ids,ids if apply else set(),set(prepared.get('excluded_ids',[])),apply)
    output=[]
    with journal.locked():
        binding_ref=prepared.get('revision_origin_ref')
        if apply and not binding_ref:
            binding_ref=journal.evidence({'schema_version':'uqmc.run-cleanup-binding.v1','run_id':prepared['run_id'],
                'plan_sha256':prepared['plan_sha256'],'source_metadata_ref':prepared['source_metadata_ref']})
        for item in selected:
            sid=item['source_id'];operation='empty-parent-'+digest({'run_id':prepared['run_id'],'source_id':sid})[:24]
            existing=[e for e in journal.records() if e['operation_id']==operation]
            get=getattr(transport,'get_trash_candidate',transport.get_file)
            raw=get(sid);original=item['before']
            common={'run_id':prepared['run_id'],'source_id':sid,'revision_manifest_ref':binding_ref,
                'prior_path':item['prior_path'],'name':original['name'],'parent_ids':original['parents'],'reason':REASON}
            if _core(raw)!=_core(original):
                output.append({'source_id':sid,'state':'SKIPPED','reason':'PARENT_IDENTITY_DRIFT'});continue
            attempted=[e for e in existing if e['kind']==PREFIX+'ATTEMPTED']
            verified=[e for e in existing if e['kind']==PREFIX+'VERIFIED']
            if raw.get('trashed') is True:
                if not attempted:output.append({'source_id':sid,'state':'SKIPPED','reason':'ALREADY_TRASHED_EXTERNALLY'});continue
                if apply and not verified:
                    before_ref=attempted[-1]['payload']['before_ref']
                    journal.append(PREFIX+'VERIFIED',operation,payload={**common,'before_ref':before_ref,
                        'observed_ref':journal.evidence(raw),'reconciled':True})
                output.append({'source_id':sid,'name':raw['name'],'state':'ALREADY_RETIRED' if verified else 'RECONCILED_TRASHED'});continue
            if verified:
                if apply:reconcile_restored(journal,factory,{sid})
                output.append({'source_id':sid,'state':'RESTORED','reason':'RESTORED_PARENT_NOT_RETRASHED'});continue
            if attempted:
                output.append({'source_id':sid,'state':'UNCERTAIN','reason':'PRIOR_TRASH_REQUEST_REQUIRES_RECONCILIATION'});continue
            if raw.get('trashed') is not False or raw.get('capabilities',{}).get('canTrash') is not True:
                output.append({'source_id':sid,'state':'SKIPPED','reason':'TRASH_CAPABILITY_UNAVAILABLE'});continue
            # Pagination/incompleteSearch handling is supplied by list_children.
            children=transport.list_children(sid)
            if children:
                output.append({'source_id':sid,'name':raw['name'],'state':'SKIPPED','reason':'PARENT_NOT_EMPTY','child_count':len(children)});continue
            if not apply:
                output.append({'source_id':sid,'name':raw['name'],'state':'READY'});continue
            before_ref=journal.evidence(raw)
            common.update(before_ref=before_ref,empty_listing_ref=journal.evidence({'parent_id':sid,'complete':True,'children':children}))
            journal.append(PREFIX+'PREPARED',operation,payload=common)
            journal.append(PREFIX+'ATTEMPTED',operation,payload=common)
            error=None
            try:
                receipt=transport.trash_file(sid)
                journal.append(PREFIX+'RECEIPT',operation,payload={**common,'receipt_ref':journal.evidence(receipt)})
            except TransportError as exc:
                error=exc.as_dict()
                journal.append(PREFIX+'UNCERTAIN',operation,payload={**common,'error':error})
            try:
                observed=get(sid)
            except TransportError as exc:
                journal.append(PREFIX+'UNCERTAIN',operation,payload={**common,'readback_error':exc.as_dict()})
                output.append({'source_id':sid,'state':'UNCERTAIN','reason':'TRASH_READBACK_UNAVAILABLE'});continue
            if _core(observed)==_core(raw) and observed.get('trashed') is True:
                journal.append(PREFIX+'VERIFIED',operation,payload={**common,'observed_ref':journal.evidence(observed),'reconciled':error is not None})
                output.append({'source_id':sid,'name':raw['name'],'state':'TRASHED'});continue
            journal.append(PREFIX+'UNCERTAIN',operation,payload={**common,'observed_ref':journal.evidence(observed),'error':error})
            output.append({'source_id':sid,'state':'UNCERTAIN','reason':'TRASH_NOT_VERIFIED','error':error})
    return {'counts':dict(Counter(x['state'] for x in output)),'items':output,'candidate_count':len(selected)}


def after_success(journal,prepared,registry,capture,factory,*,result,rollback,guard_reason,
                  current_revision,protected_ids=()):
    """Only the successful just-completed batch, while its sheet is unchanged."""
    if rollback or guard_reason or not result.get('counts') or any(n and state not in
            {'APPLIED','TARGET','COMPLETE','UNCHANGED'} for state,n in result['counts'].items()):
        return {'state':'SKIPPED','reason':'RUN_NOT_SUCCESSFUL','counts':{}}
    try:revision=current_revision()
    except Exception:return {'state':'SKIPPED','reason':'WORKBOOK_REVISION_UNAVAILABLE','counts':{}}
    if revision!=capture['source_revision']:
        return {'state':'SKIPPED','reason':'WORKBOOK_CHANGED_AFTER_RUN','counts':{}}
    return cleanup_run(journal,prepared,registry,capture,factory,apply=True,protected_ids=protected_ids)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True);parser.add_argument('--apply',action='store_true')
    parser.add_argument('--config',default='private/destination-config.json')
    args=parser.parse_args(argv)
    from .destination import command_lock,atomic_json
    c=json.loads(Path(args.config).read_text())
    with command_lock(c['run_directory']):
        j=Journal(c['store']);registry=json.loads(Path(c['registry']).read_text())
        matches=[r for r in registry.get('runs',[]) if r['run_id']==args.run_id]
        if len(matches)!=1:raise SafetyError('CLEANUP_EXACT_SAVED_RUN_REQUIRED')
        prepared=matches[0]
        if not prepared.get('revision_origin_ref'):raise SafetyError('EXPLICIT_CLEANUP_REQUIRES_FROZEN_REVISION')
        frozen=j.read_evidence(prepared['revision_origin_ref']);workbook=frozen['workbook_export']
        def factory(ids,mutations,excluded,apply):
            return GoogleDriveTransport.from_authorized_user_file(c['token'],allowed_ids=ids,mutation_ids=mutations,
                excluded_ids=excluded,apply=apply,audit_permissions=False,timeout=15)
        account=factory(set(c['anchor_paths'].values()),set(),set(c.get('excluded_ids',[])),False).get_account()
        if account['emailAddress'].casefold()!=c['expected_account'].casefold():raise SafetyError('AUTHENTICATED_ACCOUNT_MISMATCH')
        result=cleanup_run(j,prepared,registry,workbook,factory,apply=args.apply,protected_ids=set(c['anchor_paths'].values())|set(c.get('excluded_ids',[])))
        if args.apply:
            backup=Path(c['backup_directory'])/('after-empty-parent-cleanup-'+uuid.uuid4().hex)
            with j.locked():j.backup(backup)
            result['backup']=str(backup)
            atomic_json(Path(c['registry']),filter_registry(j,registry))
            atomic_json(Path(c['run_directory'])/('empty-parent-cleanup-'+uuid.uuid4().hex+'.json'),result)
        result['journal_sequence']=len(j.records());print(json.dumps(result))
    return 0


if __name__=='__main__':raise SystemExit(main())
