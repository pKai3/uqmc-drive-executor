"""Prepare and execute one approved nested destination section from a fresh sheet.

Private configuration carries the explicit owner instruction and anchor IDs.
The program never edits the workbook or sharing and never downloads file bodies.
"""
from __future__ import annotations
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import threading
import time
import uuid

from .agent_profile import agent_snapshot, agent_parent_context, register_agent_plan
from .direct_transport import GoogleDriveTransport
from .model import FOLDER, Approval, SafetyError, digest


def collect_metadata(source_ids, transport_factory, *, excluded_ids=(), workers=10, progress=None, recursive_sources=True, read_cache=None, on_read=None):
    """Read exact selected IDs and immediate parent listings once; no broad search.

    Full descendant evidence is assembled by walking selected source folders only.
    Unknown descendants halt before an individual metadata read or mutation.
    Optional preview cache: exact "get_file:<ID>" / "list_children:<ID>" keys.
    Caller validates snapshot provenance/age and must not supply it for apply.
    on_read(method, ID, value) is serialized after each successful read/cache
    update, allowing durable incremental preview capture even if a later call
    fails. Cache hits do not trigger on_read. Values are defensive copies.
    """
    selected=set(source_ids); excluded=set(excluded_ids)
    if not selected or selected & excluded:
        raise SafetyError('INVALID_OR_EXCLUDED_SECTION_READ_SCOPE')
    local=threading.local(); cache_lock=threading.Lock()
    def read(method,s,allowed):
        key=method+':'+s
        def validate(value):
            if method=='get_file':
                valid=isinstance(value,dict) and value.get('id')==s
            else:
                valid=isinstance(value,list) and all(isinstance(v,dict) and v.get('id') and
                    isinstance(v.get('parents'),list) and s in v['parents'] for v in value)
            if not valid:raise SafetyError('INVALID_SECTION_READ_CACHE:'+key)
            return copy.deepcopy(value)
        if read_cache is not None:
            with cache_lock:
                if key in read_cache:return validate(read_cache[key])
        value=getattr(client(allowed),method)(s)
        with cache_lock:
            value=validate(value)
            if read_cache is not None:read_cache[key]=copy.deepcopy(value)
            if on_read:on_read(method,s,copy.deepcopy(value))
        return value
    def client(allowed):
        key=frozenset(allowed)
        if not hasattr(local,'key') or local.key!=key:
            local.key=key; local.value=transport_factory(key)
        return local.value
    def get(s): return s,read('get_file',s,selected)
    raw={}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for s,value in pool.map(get, sorted(selected)):
            raw[s]=value
            if progress: progress({'stage':'source_metadata','checked':len(raw),'total':len(selected)})
    if any(not isinstance(v.get('parents'),list) for v in raw.values()):
        raise SafetyError('AGENT_COMPLETE_PARENT_IDS_REQUIRED')
    parents={p for v in raw.values() for p in v['parents']}
    if parents & excluded: raise SafetyError('EXCLUDED_SOURCE_PARENT')
    folders={s for s,v in raw.items() if v['mimeType']==FOLDER}
    allowed=selected|parents
    def folder(s):
        return s, raw.get(s) or read('get_file',s,allowed), read('list_children',s,allowed)
    listings={}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for s,value,children in pool.map(folder,sorted(parents|folders)):
            raw[s]=value;listings[s]=children
    # The child's exact parent IDs and this complete direct listing establish
    # the boundary relationship; the parent's own ancestor may be unavailable.
    for s in selected:
        for pid in raw[s]['parents']:
            child=next((v for v in listings[pid] if v['id']==s),None)
            if child is None or any(child.get(k)!=raw[s].get(k) for k in ('name','mimeType')) or sorted(child['parents'])!=sorted(raw[s]['parents']):
                raise SafetyError('SOURCE_PARENT_MEMBERSHIP_CONFLICT:'+s+':'+pid)
    def descendants(s,trail=()):
        if s in trail: raise SafetyError('SOURCE_FOLDER_CYCLE')
        members=[]
        for m in listings[s]:
            if m['id'] in excluded: raise SafetyError('EXCLUDED_FOLDER_DESCENDANT')
            if m['id'] not in selected: raise SafetyError('UNAPPROVED_FOLDER_DESCENDANT:'+s+':'+m['id'])
            members.append(copy.deepcopy(m))
            if m['mimeType']==FOLDER: members.extend(descendants(m['id'],trail+(s,)))
        return members
    snapshots={}
    for s,value in raw.items():
        members=descendants(s) if recursive_sources and s in folders else listings.get(s,[])
        snapshots[s]=normalize(value,members,recursive=recursive_sources and s in folders,parent_context=s not in selected)
    return {'raw':raw,'listings':listings,'metadata':snapshots,'source_ids':sorted(selected)}


def normalize(raw,members=None,*,recursive=False,parent_context=False):
    aliases={'mimeType':'mime_type','modifiedTime':'modified_time','md5Checksum':'md5_checksum','driveId':'drive_id'}
    value={aliases.get(k,k):copy.deepcopy(v) for k,v in raw.items() if k!='_http_etag'}
    value['parents_complete']=isinstance(value.get('parents'),list) if parent_context else True
    if value['mime_type']==FOLDER:
        value['subtree']={'complete':True,'membership_scope':'RECURSIVE' if recursive else 'DIRECT_CHILDREN','members':copy.deepcopy(members or [])}
    return agent_parent_context(value) if parent_context else agent_snapshot(value)


def authorize(journal,plan,metadata,instruction_ref,*,section_id):
    from .section_importer import validate_section_authority
    from .action_bridge import ActionBridge, AGENT_LIMITATIONS
    now=datetime.now(timezone.utc)
    approval=Approval(section_id+'-batch-'+uuid.uuid4().hex,plan.run_id,plan.batch_id,plan.proposal_revision,
        plan.proposal_hash,tuple(o.operation_id for o in plan.operations),'user',now.isoformat(),
        (now+timedelta(hours=4)).isoformat(),'AGENT_ASSISTED')
    exposure_ref=journal.evidence({'kind':'SCOPED_EXPOSURE_REVIEW','proposal_hash':plan.proposal_hash,
        'operation_ids':list(approval.operation_ids),'warning_presented':True,'approved':True,
        'newly_exposed_principals':[],'unknown_exposure':True,'provenance_ref':instruction_ref,
        'scope_note':'Owner accepted inherited access consequences and disabled permission auditing. Empty principals means unmeasured, not no exposure. No sharing edits.',
        'access_baselines':{s:journal.evidence(v) for s,v in sorted(metadata.items())}})
    auth={'kind':'EXPLICIT_BATCH_AUTHORIZATION','authority':'human_user','authorized':True,'actor':'user',
        'approval_hash':digest(approval.body()),'proposal_hash':plan.proposal_hash,
        'limitations_acknowledged':sorted(AGENT_LIMITATIONS),'limitations_reviewed_by':'existing direct runner controller',
        'provenance_ref':instruction_ref,'exposure_review_ref':exposure_ref,'access_policy':'SCOPED_EXPOSURE_ACCEPTED',
        'section_authority':validate_section_authority(journal,plan)}
    proof=journal.evidence({'kind':'TRUSTED_CONTROLLER_REVIEW','proposal_hash':plan.proposal_hash,
        'text':'Deterministic exact section mappings validated against fresh canonical spreadsheet and live source metadata; approved nested moves/renames only.'})
    controller=section_id+'-'+uuid.uuid4().hex
    enable={'kind':'DISPATCH_ENABLE_REVIEW','execution_transport':'DIRECT_GOOGLE_API','controller_id':controller,
        'proposal_hash':plan.proposal_hash,'approval_id':approval.approval_id,'authorization_hash':digest(auth),
        'manifest_verified':True,'reviewer':'section controller','reviewed_at':now.isoformat(),
        'expires_at':approval.expires_at,'provenance_ref':proof}
    bridge=ActionBridge(journal,mode='AGENT_ASSISTED',controller_id=controller,execution_transport='DIRECT_GOOGLE_API')
    bridge.disable_agent_dispatch('NEW_APPROVED_SECTION_CONTROLLER')
    bridge.authorize_agent_dispatch(plan,approval,auth,enable)


def config_binding(config):
    return digest({**{k:config[k] for k in ('section_id','section_path','workbook_id','expected_account','anchor_paths')},'method_profile':config.get('method_profile','approved_nested_section')})


def prepare(journal,config,transport_factory,*,apply=False,progress=None):
    from .__main__ import save_new
    from .sheets_capture import GoogleSheetsCapture
    from .importer import catalog
    from .section_prepare import select_records,resolve_destination_paths,build_preparation,exact_rename
    from .section_importer import import_section
    from .batch_source import bind_initial_section_source
    from .folder_creation import ensure_required_folder
    from .model import NATIVE,uploaded_extension
    report=progress or (lambda value:None)
    profile=config.get('method_profile','approved_nested_section')
    if profile not in {'approved_nested_section','destination_scoped_files'}:raise SafetyError('SECTION_METHOD_PROFILE_NOT_AUTHORIZED')
    root=Path(config['run_directory']);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    capture=GoogleSheetsCapture.from_authorized_user_file(config['token'],workbook_id=config['workbook_id']).capture()
    tag=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    save_new(root/('workbook-'+tag+'.json'),capture)
    source=catalog(capture,expected_workbook_id=config['workbook_id'])
    records=select_records(source,config['section_path']);ids={r['source_id'] for r in records}
    excluded=set(source['excluded_ids'])|set(config.get('excluded_ids',[]))|{e['payload']['file_id'] for e in journal.records() if e['kind']=='EXCLUDED'}
    if ids & excluded:raise SafetyError('EXCLUDED_SECTION_ID')
    done={e['payload'].get('source_id',e['payload'].get('file_id')) for e in journal.records()
          if e['kind'] in {'DESIRED_APPLIED','VERIFIED_APPLIED'}}
    if ids & done:raise SafetyError('COMPLETED_IDENTITIES_REQUIRE_HISTORY_RECONCILIATION')
    report({'stage':'fresh_sheet','identities':len(ids),'records':len(records),'source_revision':capture['source_revision']})
    def factory(allowed):return transport_factory(allowed,set(),excluded,False)
    live=collect_metadata(ids,factory,excluded_ids=excluded,workers=config.get('workers',10),progress=report,recursive_sources=profile!='destination_scoped_files')
    raw,listings,metadata=live['raw'],live['listings'],live['metadata']
    anchors=config['anchor_paths']
    anchor_ids=set(anchors.values())
    c=factory(anchor_ids)
    for sid in anchor_ids:
        if sid not in raw:raw[sid]=c.get_file(sid)
        if sid not in listings:listings[sid]=c.list_children(sid)
        metadata[sid]=normalize(raw[sid],listings[sid])
    resolution=resolve_destination_paths(records,metadata,anchors,config['section_path'],method_profile=profile)
    paths=resolution['destination_paths'];missing=resolution['missing_folders']
    # Existing matching required folders are resolved by exact parent and name;
    # generated folders from this journal retain their allocation identity.
    remaining=[]
    allocated={e['payload']['key'] for e in journal.records() if e['kind']=='REQUIRED_FOLDER_ALLOCATED' and e['payload'].get('section_id')==config['section_id']}
    for spec in missing:
        if spec['logical_key'] in allocated:
            remaining.append(spec);continue
        parent=paths.get(spec['parent_path'])
        if parent is not None:
            if parent not in listings:listings[parent]=factory({parent}).list_children(parent)
            matches=[x for x in listings[parent] if x['name']==spec['folder_name']]
            if len(matches)>1 or any(x['mimeType']!=FOLDER for x in matches):
                raise SafetyError('AMBIGUOUS_EXISTING_DESTINATION:'+spec['logical_path'])
            if matches:
                sid=matches[0]['id']
                if sid in excluded:raise SafetyError('EXCLUDED_DESTINATION')
                paths[spec['logical_path']]=sid
                raw[sid]=factory({sid}).get_file(sid);listings[sid]=factory({sid}).list_children(sid)
                metadata[sid]=normalize(raw[sid],listings[sid]);continue
        remaining.append(spec)
    report({'stage':'destination_resolution','existing_destinations':len(paths),'required_new_folders':[s['logical_path'] for s in remaining]})
    save_new(root/('metadata-'+tag+'.json'),live)
    if not apply:
        return {'preview_only':True,'source_ids':sorted(ids),'missing_folders':remaining,'destination_paths':paths}
    instruction=config['owner_instruction']
    if instruction.get('kind')!='TRUSTED_USER_INSTRUCTION' or not instruction.get('text'):
        raise SafetyError('EXPLICIT_SECTION_INSTRUCTION_REQUIRED')
    instruction_ref=journal.evidence(instruction)
    folder_specs=[];keys={x['logical_path']:x['logical_key'] for x in remaining}
    for spec in remaining:
        parent_path=spec['parent_path']
        folder_specs.append({'key':spec['logical_key'],'name':spec['folder_name'],
            **({'parent_key':keys[parent_path]} if parent_path in keys else {'parent_id':paths[parent_path]})})
    creation_ref=journal.evidence({'schema_version':'uqmc.folder-create-authority.v1','decision':'APPROVED',
        'section_id':config['section_id'],'approval_id':config['owner_approval_id'],'provenance_ref':instruction_ref,
        'access_policy':'SCOPED_INHERITANCE_ACCEPTED','folders':folder_specs})
    for spec in remaining:
        parent=paths[spec['parent_path']]
        creator=transport_factory({parent},set(),excluded,True,creation_parent_ids={parent})
        value=ensure_required_folder(journal,creator,key=spec['logical_key'],name=spec['folder_name'],parent_id=parent,authority_ref=creation_ref)
        sid=value['id'];paths[spec['logical_path']]=sid;raw[sid]=value;listings[sid]=[]
        metadata[sid]=normalize(value,[])
        report({'stage':'required_folder_verified','path':spec['logical_path'],'source_id':sid})
    capture_ref=journal.evidence({'metadata':raw,'listings':listings,'permission_audit':False})
    mapping_ref=journal.evidence({'schema_version':'uqmc.section.destination-paths.v1','section_id':config['section_id'],
        'input_sha256':source['input_sha256'],'destination_paths':paths,'provenance_ref':capture_ref})
    native,corrections={},{}
    for r in records:
        if r['action']!='RENAME':continue
        sid=r['source_id'];state=metadata[sid];title=exact_rename(r,state);mime=state['mime_type']
        if title!=r['row']['Proposed name + type suffix']:
            native[r['reference']]=journal.evidence({'schema_version':'uqmc.rename.native-display-conversion.v1',
                'source_id':sid,'mime_type':mime,'workbook_row_sha256':r['row_sha256'],
                'workbook_proposed_name':r['row']['Proposed name + type suffix'],'exact_target_name':title,
                'display_suffix':NATIVE[mime],'provenance_ref':capture_ref})
        suffix=uploaded_extension(state['name'])
        if mime not in NATIVE and mime!=FOLDER and suffix and not title.endswith(suffix):
            proof=journal.evidence({'kind':'VERIFIED_EXTENSION_CORRECTION','source_id':sid,
                'source_exact_name':state['name'],'target_exact_name':title,'mime_type':mime,'provenance_ref':capture_ref})
            corrections[r['reference']]={'source_suffix':suffix,'target_suffix':uploaded_extension(title),'mime_type':mime,'evidence_ref':proof}
    prepared=build_preparation(source,section_path=config['section_path'],section_id=config['section_id'],
        workbook_id=config['workbook_id'],metadata_by_id=metadata,destination_paths=paths,
        destination_path_evidence_ref=mapping_ref,native_conversion_refs=native,extension_corrections=corrections,method_profile=profile)
    p=prepared['projection']
    owner={'schema_version':'uqmc.section.owner-approval.v1','decision':'APPROVED','actor':'user',
        'approval_id':config['owner_approval_id'],'approved_at_utc':config['approved_at_utc'],
        'provenance':{'kind':'explicit_owner_instruction','evidence_reference':instruction_ref},
        'section_id':config['section_id'],'method_profile':p['method_profile'],'projection_revision':p['projection_revision'],
        'projection_sha256':digest(p),'member_source_ids':[m['source_id'] for m in p['members']],
        'operation_refs':prepared['selected_refs'],'allowed_actions':p['allowed_actions'],
        'allowed_destination_parent_ids':p['allowed_destination_parent_ids']}
    result=import_section(journal,capture,projection=p,owner_approval_ref=journal.evidence(owner),
        expected_section_id=config['section_id'],expected_workbook_id=config['workbook_id'],
        batch_id=config['section_id']+'-'+tag,metadata_by_id=metadata,resolutions=prepared['resolutions'],
        expected_method_profile=profile,metadata_profile='AGENT_ASSISTED')
    save_new(root/('import-'+tag+'.json'),result.manifest)
    if result.plan is None:
        raise SafetyError('SECTION_IMPORT_BLOCKED:'+json.dumps(result.manifest.get('section_reason_code') or
            [r for r in result.manifest['rows'] if r['state']=='BLOCKED']))
    plan=result.plan;ids={o.file_id for o in plan.operations};register_agent_plan(journal,plan)
    bind_initial_section_source(journal,plan,capture)
    required={o.file_id for o in plan.operations}
    for o in plan.operations: required |= set(o.before['parents'])|set(o.target.get('parents',[]))
    authorize(journal,plan,{s:metadata[s] for s in required},instruction_ref,section_id=config['section_id'])
    plan_path=save_new(root/('frozen-plan-'+tag+'.json'),plan.body())
    output={'config_binding_sha256':config_binding(config),'plan_path':plan_path,'plan_sha256':plan.proposal_hash,'run_id':plan.run_id,'source_ids':sorted(ids),
        'allowed_ids':sorted(required),'source_revision':capture['source_revision'],'destination_paths':paths,
        'new_folder_count':len(remaining),'source_metadata_ref':capture_ref,'method_profile':profile}
    save_new(root/'prepared-batch.json',output)
    report({'stage':'prepared','operations':len(plan.operations),'identities':len(ids),'plan_sha256':plan.proposal_hash})
    return output


def main(argv=None):
    import argparse,os
    from .__main__ import save_new,plan_from_json
    from .journal import Journal
    from .batch_source import require_batch_source
    from .desired import DesiredRunner,export_status
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--resume',action='store_true')
    args=parser.parse_args(argv);os.umask(0o077)
    config=json.loads(Path(args.config).read_text());root=Path(config['run_directory'])
    journal=Journal(config['store']);started=time.monotonic()
    def report(value):print(json.dumps(value),flush=True)
    def factory(allowed,mutations,excluded,apply,**kw):
        return GoogleDriveTransport.from_authorized_user_file(config['token'],apply=apply,allowed_ids=allowed,
            mutation_ids=mutations,excluded_ids=excluded,audit_permissions=False,**kw)
    try:
        with journal.locked():
            c=factory(set(config['anchor_paths'].values()),set(),set(config.get('excluded_ids',[])),False)
            account=c.get_account()
            if account['emailAddress'].casefold()!=config['expected_account'].casefold():raise SafetyError('AUTHENTICATED_ACCOUNT_MISMATCH')
            report({'stage':'account_verified','account':account['emailAddress']})
            prepared_path=root/'prepared-batch.json'
            if prepared_path.exists():
                if not args.resume:raise SafetyError('PREPARED_BATCH_EXISTS_USE_EXPLICIT_RESUME')
                prepared=json.loads(prepared_path.read_text())
                if prepared.get('config_binding_sha256')!=config_binding(config):raise SafetyError('PREPARED_BATCH_CONFIG_MISMATCH')
            else:
                if args.apply:
                    before=Path(config['backup_directory'])/('before-section-'+str(uuid.uuid4()))
                    journal.backup(before)
                    report({'stage':'before_backup','path':str(before),'journal_sequence':len(journal.records())})
                prepared=prepare(journal,config,factory,apply=args.apply,progress=report)
            if not args.apply:report(prepared);return 0
            plan=plan_from_json(json.loads(Path(prepared['plan_path']).read_text()))
            if plan.proposal_hash!=prepared['plan_sha256']:raise SafetyError('PREPARED_PLAN_HASH_MISMATCH')
            require_batch_source(journal,plan,resume=args.resume)
            excluded=set(config.get('excluded_ids',[]))|set(plan.excluded_ids)
            def client():return factory(set(prepared['allowed_ids']),set(prepared['source_ids']),excluded,True)
        runner=DesiredRunner(journal,plan,client(),audit_permissions=False,max_attempts=3,progress=report)
        signal.signal(signal.SIGINT,lambda *_:setattr(runner,'stop_requested',True))
        signal.signal(signal.SIGTERM,lambda *_:setattr(runner,'stop_requested',True))
        dispatch=time.monotonic()
        result=runner.run(apply=True,confirm_plan_sha256=plan.proposal_hash,source_ids=prepared['source_ids'],
            limit=len(prepared['source_ids']),workers=config.get('workers',10),transport_factory=client)
        with journal.locked():
            result['execution_seconds']=round(time.monotonic()-dispatch,3)
            result['elapsed_seconds']=round(time.monotonic()-started,3)
            result_path=save_new(root/('result-'+str(uuid.uuid4())+'.json'),result)
            exports=export_status(journal,result,root/'exports')
            backup=Path(config['backup_directory'])/('section-complete-'+str(uuid.uuid4()));journal.backup(backup)
            if (backup/'events.jsonl').read_bytes()!=journal.path.read_bytes():raise SafetyError('BACKUP_VERIFICATION_FAILED')
            latest=journal.records()[-1];done=[i for i in result['items'] if i['state'] in {'APPLIED','TARGET'}]
            final={'section':config['section_path'],'counts':result['counts'],'verified_identities':len(done),
                'total_identities':len(result['items']),'verified_rename_effects':sum(i.get('effects',{}).get('rename')=='APPLIED' for i in done),
                'verified_move_effects':sum(i.get('effects',{}).get('move')=='APPLIED' for i in done),
                'blockers':[i for i in result['items'] if i['state'] not in {'APPLIED','TARGET'}],
                'new_folder_count':prepared['new_folder_count'],'folder_id':prepared['destination_paths'][config['section_path']],
                'execution_seconds':result['execution_seconds'],'elapsed_seconds':round(time.monotonic()-started,3),
                'journal_sequence':latest['seq'],'journal_tip_sha256':latest['hash'],'backup':str(backup),'backup_verified':True,
                'result_path':result_path,'exports':exports,'spreadsheet_published':False,'html_published':False}
            save_new(root/('completion-'+str(uuid.uuid4())+'.json'),final);report(final)
            return 0 if len(done)==len(result['items']) else 2
    except Exception as exc:
        from .direct_transport import TransportError
        report({'error':str(exc) if isinstance(exc,SafetyError) else type(exc).__name__,
            **({'request_error':exc.as_dict()} if isinstance(exc,TransportError) else {}),
            'journal_sequence':len(journal.records()),'elapsed_seconds':round(time.monotonic()-started,3)})
        return 2


if __name__=='__main__':raise SystemExit(main())
