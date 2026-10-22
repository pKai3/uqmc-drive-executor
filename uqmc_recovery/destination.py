"""One canonical destination path -> fresh proposal, bounded execution and publication.

Default is read-only. --apply is the operator's explicit instruction for this
section, including only necessary destination folders. Credentials are supplied
by a private one-time project configuration, never inferred or created here.
"""
from __future__ import annotations
import argparse
import copy
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import threading
import time
import uuid

from .__main__ import save_new, plan_from_json
from .model import FOLDER, NATIVE, MIME_EXTENSIONS, SafetyError, digest, uploaded_extension
from .section_prepare import _path, exact_rename
from .section_execution import collect_metadata, normalize, authorize
from .destination_planner import plan_destination, build_hybrid_preparation, logical_parent, file_parent_path, select_destination_records as select_records
from .destination_history import build_history_index, reconcile_item
from .folder_creation import reject_trailing_whitespace_aliases


def utc(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    save_new(tmp,value);os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


@contextmanager
def command_lock(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(directory/'.destination-command.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SafetyError('ANOTHER_DESTINATION_COMMAND_IS_RUNNING') from None
        yield
    finally:os.close(fd)


@contextmanager
def timed_stage(report, name):
    started = time.monotonic()
    report({'stage': 'stage_started', 'stage_name': name})
    outcome = 'FAILED'
    try:
        yield
        outcome = 'COMPLETE'
    finally:
        report({'stage': 'stage_finished', 'stage_name': name,
                'duration_seconds': round(time.monotonic() - started, 3), 'outcome': outcome})


def section_key(path):return 'destination-'+digest({'canonical_path':_path(path)})[:20]


def effective_blocked_sources(source):
    """An identity's withdrawn/No action cannot be bypassed by another row."""
    from .destination_planner import _blocked_ids
    return _blocked_ids(source)


def semantic_selection(source, section):
    """Resume compares execution authority too; unrelated edits stay unrelated."""
    blocked=effective_blocked_sources(source)
    return {r['reference']:{'id':r['source_id'],'action':r['action'],'decision':r['decision'],
        'path':r['row'].get('Exact target path',r['row'].get('Proposed destination folder')),
        'name':r['row'].get('Proposed name + type suffix'),
        'source_excluded':r['source_id'] in blocked} for r in select_records(source,section)}


def validate_resume_scope(source,section,prepared,journal):
    """Do not re-enable a stored batch after source authority was removed."""
    records=journal.records()
    excluded=effective_blocked_sources(source)|{e['payload']['file_id'] for e in records if e['kind']=='EXCLUDED'}
    refs={e['payload']['proposal_ref'] for e in records if e['kind']=='PROPOSAL_EXCLUDED'}
    selected=set(prepared['selected_source_ids'])
    if selected & excluded:
        raise SafetyError('RESUME_SOURCE_AUTHORITY_REMOVED:'+','.join(sorted(selected & excluded)))
    affected={r['source_id'] for r in source['records'].values()
        if r['reference'] in refs or set(r.get('proposal_refs',[])) & refs}
    if selected & affected:
        raise SafetyError('RESUME_SOURCE_AUTHORITY_REMOVED:'+','.join(sorted(selected & affected)))
    if semantic_selection(source,section)!=prepared['semantic_selection']:
        raise SafetyError('PENDING_PROPOSAL_CHANGED_START_FRESH_BATCH_AFTER_RECONCILING_OLD_REQUESTS')


def recursive_selected(live):
    """Prove intact reuse without reading unassigned descendant bodies/metadata.

    Unknown children are included in membership, then disqualify whole-folder
    reuse. Traversal only recurses into already-selected, already-listed folders.
    """
    raw,listings,states=live['raw'],live['listings'],live['metadata']
    def members(sid,trail=()):
        if sid in trail:raise SafetyError('SOURCE_FOLDER_CYCLE:'+sid)
        out=[]
        for child in listings.get(sid,[]):
            c=copy.deepcopy(child);c['mime_type']=c.pop('mimeType',c.get('mime_type'))
            out.append(c)
            if c['mime_type']==FOLDER and c['id'] in live['source_ids']:
                out.extend(members(c['id'],trail+(sid,)))
        return out
    for sid in live['source_ids']:
        if raw[sid]['mimeType']==FOLDER:
            states[sid]=normalize(raw[sid],members(sid),recursive=True)
    return live


def normalize_history(value):
    from .agent_profile import agent_snapshot
    return agent_snapshot(value) if 'mime_type' in value else normalize(value)



def proposal_original_name(rename_record, move_record, state):
    """Resolve a display label only with corroborated original-title evidence.

    Uploaded display suffixes require a same-ID manifest title, both source
    paths, and exact MIME/suffix agreement. Native whitespace handling retains
    its existing independent manifest proof. Neither branch changes the target
    title, invents an original name or accepts unrelated live drift.
    """
    old=rename_record['row']['Current name + type suffix']
    suffix=NATIVE.get(state['mime_type']);current=state['name']
    if current==old:return old
    if not suffix:
        if (move_record and rename_record.get('source_id')==move_record.get('source_id')==state.get('id')
                and isinstance(state.get('id'),str) and state['id']):
            row=move_record['row'];exact=row.get('Current item')
            paths=(row.get('Source path'),rename_record['row'].get('Current source path'))
            if (isinstance(exact,str) and exact and current==exact
                    and all(isinstance(path,str) and path.endswith('/'+exact) for path in paths)
                    and any(old==exact+ext for ext in MIME_EXTENSIONS.get(state['mime_type'],set()))):
                return exact
        return old
    stripped=old[:-len(suffix)] if old.endswith(suffix) else old
    if current==stripped:return stripped
    if move_record:
        row=move_record['row'];exact=row.get('Current item');source_path=row.get('Source path')
        if (isinstance(exact,str) and exact and isinstance(source_path,str)
                and current==exact and source_path.endswith('/'+exact)
                and old==exact.strip()+suffix):
            return exact
    return stripped


def read_and_plan(config, section, journal, capture, factory, registry, report, *, preview=False):
    from .importer import catalog
    source=catalog(capture,expected_workbook_id=config['workbook_id'],include_subtree_coverage=True,subtree_section_path=section)
    history=journal.records()
    # A later blank cell does not reopen a durably excluded source/proposal.
    # Preserve the capture hash; these exclusions are separate journal authority.
    source['excluded_ids']=sorted(set(source['excluded_ids'])|{e['payload']['file_id'] for e in history if e['kind']=='EXCLUDED'})
    source['excluded_refs']=sorted(set(source['excluded_refs'])|{e['payload']['proposal_ref'] for e in history if e['kind']=='PROPOSAL_EXCLUDED'})
    records=select_records(source,section)
    excluded=set(config.get('excluded_ids',[]))|set(source['excluded_ids'])
    # A No/withdrawn action excludes that identity from the hybrid section.
    bad=effective_blocked_sources(source)
    ids={r['source_id'] for r in records}-bad
    if not ids:raise SafetyError('NO_ELIGIBLE_ITEMS_FOR_DESTINATION:'+section)
    if ids & excluded:raise SafetyError('EXCLUDED_SECTION_ID')
    report({'stage':'fresh_sheet','section':section,'selected_identities':len(ids),'source_revision':capture['source_revision']})
    cache={};cache_dir=None
    if preview:
        cache_dir=Path(config['run_directory'])/'preview-reads'/(section_key(section)+'-'+digest({'input':source['input_sha256'],'journal_sequence':len(journal.records())})[:24])
        cache_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        now=time.time()
        for saved in cache_dir.glob('*.json'):
            value=json.loads(saved.read_text())
            if 0<=now-value['read_at_unix']<=300 and digest(value['value'])==value['value_sha256']:
                cache[value['key']]=value['value']
    reused_cache=len(cache)
    def on_read(method,sid,value):
        if cache_dir is not None:
            key=method+':'+sid
            atomic_json(cache_dir/(digest(key)+'.json'),{'key':key,'read_at_unix':time.time(),'value_sha256':digest(value),'value':value})
    class RetryRead:
        def __init__(self,client):self.client=client
        def __getattr__(self,key):return getattr(self.client,key)
        def call(self,method,sid):
            from .direct_transport import TransportError
            for attempt in (1,2):
                try:return getattr(self.client,method)(sid)
                except TransportError as exc:
                    if attempt==2 or not exc.transient:raise
                    report({'stage':'read_retry','method':method,'source_id':sid,'attempt':2,'reason':exc.code})
        def get_file(self,sid):return self.call('get_file',sid)
        def list_children(self,sid):return self.call('list_children',sid)
    read=lambda allowed:RetryRead(factory(allowed,set(),excluded,False))
    histories=build_history_index(journal)
    known=dict(config['anchor_paths'])
    for entry in registry.get('sections',[]):known.update(entry.get('destination_paths',{}))
    by_id={}
    for r in records:by_id.setdefault(r['source_id'],{})[r['action']]=r
    cached={}
    for sid in sorted(ids):
        h=histories.get(sid);actions=by_id[sid]
        if not h or not h.get('latest_observation') or h.get('pending_requests') or h.get('rolled_back_fields'):continue
        prior=h['latest_observation'];baseline=h['baseline']
        if prior['mime_type']==FOLDER:continue
        rename=actions.get('RENAME');move=actions.get('MOVE')
        parent_path=file_parent_path(move,rename,normalize_history(baseline))
        name=exact_rename(rename,normalize_history(baseline)) if rename else prior['name']
        if known.get(parent_path) not in prior['parents'] or [known.get(parent_path)]!=prior['parents'] or name!=prior['name']:continue
        if not h['completed_fields']:continue
        cached[sid]=copy.deepcopy(baseline)
    read_ids=ids-cached.keys()
    live=collect_metadata(read_ids,read,excluded_ids=excluded,workers=config.get('workers',10),progress=report,recursive_sources=False,read_cache=cache if preview else None,on_read=on_read if preview else None) if read_ids else {'raw':{},'listings':{},'metadata':{},'source_ids':[]}
    for sid,baseline in cached.items():
        raw_value=copy.deepcopy(baseline)
        for normalized,raw_key in {'mime_type':'mimeType','modified_time':'modifiedTime','md5_checksum':'md5Checksum','drive_id':'driveId'}.items():
            if normalized in raw_value:raw_value[raw_key]=raw_value.pop(normalized)
        live['raw'][sid]=raw_value;live['metadata'][sid]=normalize(raw_value)
    live['source_ids']=sorted(ids);live['history_cached_source_ids']=sorted(cached);live['preview_cached_read_count']=reused_cache
    live=recursive_selected(live)
    report({'stage':'verified_history_skipped','identities':len(cached),'remaining_live_identities':len(read_ids),'preview_cached_reads_available':reused_cache})
    raw,listings,metadata=live['raw'],live['listings'],live['metadata']
    def folder_read(method,sid):
        key=method+':'+sid
        if preview and key in cache:return copy.deepcopy(cache[key])
        value=getattr(read({sid}),method)(sid)
        if preview:cache[key]=copy.deepcopy(value);on_read(method,sid,value)
        return value
    def folder(sid, *, parent_context=False):
        if sid in excluded:raise SafetyError('EXCLUDED_DESTINATION')
        if sid not in raw or sid not in listings:report({'stage':'destination_metadata','source_id':sid})
        if sid not in raw:raw[sid]=folder_read('get_file',sid)
        if raw[sid]['mimeType']!=FOLDER or raw[sid].get('trashed'):raise SafetyError('DESTINATION_NOT_ACTIVE_FOLDER:'+sid)
        if sid not in listings:listings[sid]=folder_read('list_children',sid)
        if sid not in metadata or (not parent_context and metadata[sid].get('observation_profile')=='READ_ONLY_PARENT_CONTEXT'):
            metadata[sid]=normalize(raw[sid],listings[sid],parent_context=parent_context)
    # Project anchors are verified once per invocation. Deep anchors may only
    # come from a previous command's saved exact destination mapping.
    known=dict(config['anchor_paths'])
    for entry in registry.get('sections',[]):known.update(entry.get('destination_paths',{}))
    anchor=logical_parent(section)
    ancestors=[p for p in known if anchor==p or anchor.startswith(p+'/')]
    if not ancestors and not config.get('root_id'):raise SafetyError('CONFIGURE_PROJECT_ROOT_ANCHOR_FOR:'+anchor)
    start=max(ancestors,key=len) if ancestors else '';sid=known[start] if start else config['root_id']
    # An explicitly configured boundary anchor needs no guessed grandparent.
    # A top-level anchor with a configured project root still proves placement.
    anchor_requires_parent=bool(start and '/' not in start and config.get('root_id'))
    folder(sid,parent_context=sid not in ids and not anchor_requires_parent)
    if start and raw[sid]['name'].strip()!=start.rsplit('/',1)[-1].strip():raise SafetyError('PROJECT_ANCHOR_NAME_DRIFT:'+start)
    if start and '/' not in start and config.get('root_id') and raw[sid]['parents']!=[config['root_id']]:
        raise SafetyError('PROJECT_ANCHOR_PARENT_DRIFT:'+start)
    path=start
    for segment in anchor[len(start):].strip('/').split('/') if anchor!=start else []:
        matches=[c for c in listings[sid] if c['name'].strip()==segment]
        if len(matches)!=1 or matches[0]['mimeType']!=FOLDER:raise SafetyError('DESTINATION_PARENT_MISSING_OR_AMBIGUOUS:'+path+'/'+segment)
        sid=matches[0]['id'];path=(path+'/' if path else '')+segment;folder(sid)
    paths={anchor:sid}
    for p,s in known.items():
        if p==section or p.startswith(section+'/'):
            folder(s);paths[p]=s
    layout=plan_destination(source,section_path=section,metadata_by_id=metadata,anchor_paths=paths)
    # Discover existing exact destination containers before considering creation.
    # Do not adopt an unrelated same-name file or collapse duplicate folders.
    allocated={e['payload']['key'] for e in journal.records() if e['kind']=='REQUIRED_FOLDER_ALLOCATED' and e['payload'].get('section_id')==section_key(section)}
    candidates=[{'logical_path':p,'logical_key':'folder:'+digest({'section_path':section,'logical_path':p}),'parent_path':logical_parent(p),'folder_name':n}
                for p,n in sorted(layout['destination_folder_names'].items(),key=lambda kv:(kv[0].count('/'),kv[0]))]
    for spec in candidates:
        if spec['logical_key'] in allocated and spec['logical_path'] not in paths:continue
        parent=layout['destination_paths'].get(spec['parent_path'])
        if parent is None:continue
        folder(parent,parent_context=parent==paths[anchor] and parent not in ids)
        reject_trailing_whitespace_aliases(listings[parent],spec['folder_name'],context=spec['logical_path'])
        matches=[c for c in listings[parent] if c['name']==spec['folder_name']]
        if len(matches)>1 or any(c['mimeType']!=FOLDER for c in matches):raise SafetyError('AMBIGUOUS_EXISTING_DESTINATION:'+spec['logical_path'])
        if matches:
            found=matches[0]['id'];folder(found)
            paths[spec['logical_path']]=found
            layout=plan_destination(source,section_path=section,metadata_by_id=metadata,anchor_paths=paths)
    for logical,folder_id in layout['destination_paths'].items():
        if logical==anchor or folder_id in layout['reused_folder_ids']:continue
        expected_parent=layout['destination_paths'].get(logical_parent(logical))
        if raw[folder_id]['name']!=layout['destination_folder_names'][logical] or raw[folder_id]['parents']!=[expected_parent]:
            raise SafetyError('EXISTING_DESTINATION_LOCATION_OR_NAME_DRIFT:'+logical)
    checks=[]
    records_by_id={}
    for r in records:records_by_id.setdefault(r['source_id'],{})[r['action']]=r
    for sid,route in layout['item_routes'].items():
        if sid in layout['source_container_ids']:continue
        parent=layout['destination_paths'].get(route['parent_path'])
        unresolved=parent is None
        if unresolved:parent='unresolved:'+digest(route['parent_path'])
        desired={'id':sid,'name':route['exact_name'],'parents':[parent],'mime_type':metadata[sid]['mime_type']}
        baseline=copy.deepcopy(metadata[sid]);r=records_by_id[sid].get('RENAME')
        if not histories.get(sid) and r:
            baseline['name']=proposal_original_name(r,records_by_id[sid].get('MOVE'),baseline)
        check=reconcile_item(histories.get(sid),desired,raw[sid],baseline=baseline,logical_destination=route['parent_path'])
        check['destination_unresolved']=unresolved
        check['verification_basis']='SAVED_VERIFIED_HISTORY' if sid in cached else 'CURRENT_READ'
        caps=raw[sid].get('capabilities',{})
        if check['disposition']!='BLOCKED' and 'name' in check.get('pending_fields',[]) and caps.get('canRename') is False:
            check.update(disposition='BLOCKED',reason='SOURCE_CANNOT_RENAME')
        if check['disposition']!='BLOCKED' and 'parents' in check.get('pending_fields',[]):
            parent_raw=raw.get(parent,{})
            field='canMoveItemOutOfDrive' if raw[sid].get('driveId')!=parent_raw.get('driveId') else 'canMoveItemWithinDrive'
            if caps.get(field) is False:check.update(disposition='BLOCKED',reason='SOURCE_CANNOT_MOVE')
            elif parent_raw.get('capabilities',{}).get('canAddChildren') is False:check.update(disposition='BLOCKED',reason='DESTINATION_CANNOT_ADD_CHILDREN')
        checks.append(check)
    # Exact live collision checks use captured destination listings, never filenames as identity.
    moving={s:route for s,route in layout['item_routes'].items() if s not in layout['source_container_ids']}
    for check in checks:
        if check['disposition']=='BLOCKED' or check['destination_unresolved']:continue
        target=check['desired'];parent=target['parents'][0]
        for child in listings.get(parent,[]):
            if child['id']==check['source_id'] or child['name']!=target['name']:continue
            other=moving.get(child['id']);other_parent=layout['destination_paths'].get(other['parent_path']) if other else None
            if other and (other_parent!=parent or other['exact_name']!=target['name']):
                # The runner orders actual occupancy conservatively; it will not overwrite.
                continue
            check.update(disposition='BLOCKED',reason='LIVE_EXACT_NAME_COLLISION:'+child['id'])
    return {'source':source,'capture':capture,'live':live,'layout':layout,'checks':checks,'excluded':excluded,
            'semantic_selection':semantic_selection(source,section)}


def freeze(config, section, journal, state, factory, run_dir, report):
    from .folder_creation import ensure_required_folder
    from .section_importer import import_section
    from .agent_profile import register_agent_plan
    from .batch_source import bind_initial_section_source
    from .batch_source import _age
    _age({'captured_at_utc':state['capture']['exported_at_utc']})
    sid=section_key(section);approval_id=sid+'-'+uuid.uuid4().hex;stamp=utc()
    instruction_ref=journal.evidence({'kind':'TRUSTED_USER_INSTRUCTION','text':section,
        'provenance':'Operator invoked destination CLI with --apply for this exact destination subtree.',
        'access_policy':'SCOPED_INHERITANCE_ACCEPTED','no_permission_changes':True})
    layout=state['layout'];paths=dict(layout['destination_paths']);live=state['live'];metadata=live['metadata']
    missing=layout['missing_folders'];keys={s['logical_path']:s['logical_key'] for s in missing}
    specs=[{'key':s['logical_key'],'name':s['folder_name'],**({'parent_key':keys[s['parent_path']]} if s['parent_path'] in keys else {'parent_id':paths[s['parent_path']]})} for s in missing]
    creation_ref=journal.evidence({'schema_version':'uqmc.folder-create-authority.v1','decision':'APPROVED','section_id':sid,
        'approval_id':approval_id,'provenance_ref':instruction_ref,'access_policy':'SCOPED_INHERITANCE_ACCEPTED','folders':specs})
    for spec in missing:
        parent=paths[spec['parent_path']]
        client=factory({parent},set(),state['excluded'],True,creation_parent_ids={parent})
        raw=ensure_required_folder(journal,client,key=spec['logical_key'],name=spec['folder_name'],parent_id=parent,authority_ref=creation_ref)
        paths[spec['logical_path']]=raw['id'];live['raw'][raw['id']]=raw;live['listings'][raw['id']]=[];metadata[raw['id']]=normalize(raw,[])
        report({'stage':'folder_verified','path':spec['logical_path'],'source_id':raw['id']})
    evidence_ref=journal.evidence({'metadata':live['raw'],'listings':live['listings'],'permission_audit':False,
        'history_cached_source_ids':live.get('history_cached_source_ids',[])})
    for logical,folder_id in paths.items():
        if logical==logical_parent(section):continue
        value=metadata[folder_id];parent_path=logical_parent(logical)
        expected=layout['destination_folder_names'].get(logical,logical.rsplit('/',1)[-1])
        if value['name']==expected and value['parents']==[paths[parent_path]]:
            journal.append('DESTINATION_VERIFIED',payload={'section_id':sid,'file_id':folder_id,'exact_name':value['name'],
                'parent_ids':value['parents'],'proposal_path':logical,'observed_ref':journal.evidence(value)})
    mapping_ref=journal.evidence({'schema_version':'uqmc.section.destination-paths.v1','section_id':sid,
        'input_sha256':state['source']['input_sha256'],'destination_paths':paths,'provenance_ref':evidence_ref})
    native,corrections={},{}
    for r in layout['records']:
        if r['action']!='RENAME' or r['source_id'] in layout['source_container_ids']:continue
        before=metadata[r['source_id']];name=exact_rename(r,before);mime=before['mime_type']
        if name!=r['row']['Proposed name + type suffix']:
            native[r['reference']]=journal.evidence({'schema_version':'uqmc.rename.native-display-conversion.v1','source_id':before['id'],'mime_type':mime,
                'workbook_row_sha256':r['row_sha256'],'workbook_proposed_name':r['row']['Proposed name + type suffix'],
                'exact_target_name':name,'display_suffix':NATIVE[mime],'provenance_ref':evidence_ref})
        suffix=uploaded_extension(before['name'])
        if mime not in NATIVE and mime!=FOLDER and suffix and not name.endswith(suffix):
            proof=journal.evidence({'kind':'VERIFIED_EXTENSION_CORRECTION','source_id':before['id'],'source_exact_name':before['name'],
                'target_exact_name':name,'mime_type':mime,'provenance_ref':evidence_ref})
            corrections[r['reference']]={'source_suffix':suffix,'target_suffix':uploaded_extension(name),'mime_type':mime,'evidence_ref':proof}
    prepared=build_hybrid_preparation(state['source'],section_path=section,section_id=sid,workbook_id=config['workbook_id'],
        metadata_by_id=metadata,destination_paths=paths,destination_path_evidence_ref=mapping_ref,native_conversion_refs=native,extension_corrections=corrections)
    p=prepared['projection']
    owner={'schema_version':'uqmc.section.owner-approval.v1','decision':'APPROVED','actor':'user','approval_id':approval_id,'approved_at_utc':stamp,
        'provenance':{'kind':'explicit_owner_instruction','evidence_reference':instruction_ref},'section_id':sid,'method_profile':p['method_profile'],
        'projection_revision':p['projection_revision'],'projection_sha256':digest(p),'member_source_ids':[m['source_id'] for m in p['members']],
        'operation_refs':prepared['selected_refs'],'allowed_actions':p['allowed_actions'],'allowed_destination_parent_ids':p['allowed_destination_parent_ids']}
    result=import_section(journal,state['capture'],projection=p,owner_approval_ref=journal.evidence(owner),expected_section_id=sid,
        expected_workbook_id=config['workbook_id'],batch_id=sid+'-'+uuid.uuid4().hex[:12],metadata_by_id=metadata,resolutions=prepared['resolutions'],
        expected_method_profile='destination_hybrid',metadata_profile='AGENT_ASSISTED')
    if result.plan is None:raise SafetyError('SECTION_IMPORT_BLOCKED:'+str(result.manifest.get('section_reason_code',result.manifest.get('rows'))))
    plan=result.plan;register_agent_plan(journal,plan);bind_initial_section_source(journal,plan,state['capture'])
    allowed={o.file_id for o in plan.operations}|{s for o in plan.operations for s in o.before['parents']+o.target.get('parents',[])}
    authorize(journal,plan,{s:metadata[s] for s in allowed},instruction_ref,section_id=sid)
    plan_path=save_new(run_dir/'plan.json',plan.body())
    pending=[x['source_id'] for x in state['checks'] if x['disposition']!='COMPLETE']
    files=[s for s in prepared['source_ids'] if metadata[s]['mime_type']!=FOLDER]
    output={'run_id':plan.run_id,'plan_sha256':plan.proposal_hash,'plan_path':plan_path,'section_path':section,'section_id':sid,
        'selected_source_ids':pending,'allowed_ids':sorted(allowed),'excluded_ids':sorted(state['excluded']),
        'destination_paths':paths,'semantic_selection':state['semantic_selection'],'source_revision':state['capture']['source_revision'],
        'source_ids':files,'created_folder_keys':keys,'source_container_ids':layout['source_container_ids'],'instruction_ref':instruction_ref,
        'source_metadata_ref':evidence_ref,'new_folder_count':len(missing),
        'subtree_coverage_version':state['source'].get('subtree_coverage_version')}
    save_new(run_dir/'prepared.json',output);return output


class RevisionGuard:
    def __init__(self,capture,revision,report,interval=30):
        self.capture,self.revision,self.report,self.interval=capture,revision,report,interval
        self.last=time.monotonic();self.lock=threading.Lock();self.reason=None;self.runner=None
    def __call__(self,event):
        self.report(event)
        with self.lock:
            if self.runner is None or self.reason or time.monotonic()-self.last<self.interval:return
            self.last=time.monotonic()
            try:
                if self.capture._revision()!=self.revision:self.reason='WORKBOOK_CHANGED_REFRESH_REQUIRED'
            except Exception:self.reason='WORKBOOK_REVISION_CHECK_UNAVAILABLE'
            if self.reason:
                self.runner.stop_requested=True;self.report({'stage':'scheduling_stopped','reason':self.reason})


def run_destination(config,section,*,apply=False,resume=False,rollback=None,publish_only=None,
                    capture_client=None,factory=None,publisher=None,verify_completed=None,report=lambda x:None):
    from .journal import Journal
    from .importer import catalog
    from .desired import DesiredRunner,export_status
    from .sheets_capture import GoogleSheetsCapture
    from .direct_transport import GoogleDriveTransport
    from .destination_publication import publish_registered_sections,resume_publication,GooglePublicationDrive
    section=_path(section);started=time.monotonic()
    if not config.get('accepted_inherited_access'):raise SafetyError('PROJECT_INHERITED_ACCESS_POLICY_NOT_CONFIGURED')
    if factory is None:
        def factory(allowed,mutations,excluded,enabled,**kw):
            return GoogleDriveTransport.from_authorized_user_file(config['token'],allowed_ids=allowed,mutation_ids=mutations,
                excluded_ids=excluded,apply=enabled,audit_permissions=False,timeout=15,**kw)
    if capture_client is None:capture_client=GoogleSheetsCapture.from_authorized_user_file(config['token'],workbook_id=config['workbook_id'],timeout=15)
    with command_lock(config['run_directory']):
        journal=Journal(config['store']);registry_path=Path(config['registry']);registry=json.loads(registry_path.read_text()) if registry_path.exists() else {'sections':[],'runs':[]}
        account=factory(set(config['anchor_paths'].values()),set(),set(config.get('excluded_ids',[])),False).get_account()
        if account['emailAddress'].casefold()!=config['expected_account'].casefold():raise SafetyError('AUTHENTICATED_ACCOUNT_MISMATCH')
        report({'stage':'account_verified','account':account['emailAddress']})
        if publish_only:
            if not apply:raise SafetyError('PUBLICATION_RETRY_REQUIRES_APPLY')
            drive=GooglePublicationDrive(factory({config['viewer_file_id']},{config['viewer_file_id']},set(),True),config['viewer_file_id'],apply=True)
            return resume_publication(publish_only,sheet_capture=capture_client,drive=drive,apply=True,
                                      server_sync=config.get('server_sync'))
        capture=capture_client.capture();source=catalog(capture,expected_workbook_id=config['workbook_id'],include_subtree_coverage=True,subtree_section_path=section)
        if not apply:atomic_json(Path(config['run_directory'])/(section_key(section)+'-preview-workbook.json'),capture)
        if verify_completed is not None:
            if apply or resume or rollback:raise SafetyError('COMPLETED_SAMPLE_IS_READ_ONLY')
            return verify_completed_sample(config,section,journal,source,factory,registry,verify_completed)
        old=[r for r in registry.get('runs',[]) if r['section_path']==section]
        prepared=None
        if rollback or resume:
            matches=[r for r in old if not rollback or r['run_id']==rollback]
            if not matches:raise SafetyError('NO_SAVED_DESTINATION_RUN_FOR_RECOVERY')
            prepared=matches[-1]
            if prepared.get('subtree_coverage_version') != 1:
                source=catalog(capture,expected_workbook_id=config['workbook_id'])
            if not rollback:validate_resume_scope(source,section,prepared,journal)
        def publish_current():
            try:
                if publisher is not None:return publisher(registry)
                from .publication_projection import ReadOnlyJournal
                fresh=capture_client.capture()
                drive=GooglePublicationDrive(factory({config['viewer_file_id']},{config['viewer_file_id']},set(),True),config['viewer_file_id'],apply=True)
                return publish_registered_sections(config,registry,journal=ReadOnlyJournal(config['store']),fresh_workbook=fresh,
                    sheet_capture=capture_client,drive=drive,apply=True)
            except Exception as exc:
                from .destination_publication import _error
                detail = _error(exc)
                return {'state':'PENDING','error':str(exc) if isinstance(exc,SafetyError) else detail.get('reason',type(exc).__name__),
                        'error_class':type(exc).__name__}
        if not prepared:
            with timed_stage(report,'Source and destination checks'), journal.locked():
                state=read_and_plan(config,section,journal,capture,factory,registry,report,preview=not apply)
            blocked=[x for x in state['checks'] if x['disposition']=='BLOCKED']
            pending=[x for x in state['checks'] if x['disposition']!='COMPLETE']
            summary={'section':section,'preview_only':not apply,'counts':dict(Counter(x['disposition'] for x in state['checks'])),
                'missing_folders':[x['logical_path'] for x in state['layout']['missing_folders']],
                'whole_folder_reuse_ids':state['layout']['reused_folder_ids'],'retained_source_folder_ids':state['layout']['source_container_ids'],
                'skipped_verified_history':len(state['live'].get('history_cached_source_ids',[])),
                'preview_cached_reads_available':state['live'].get('preview_cached_read_count',0),
                'required_rename_effects':sum('name' in x.get('pending_fields',[]) for x in pending),
                'required_move_effects':sum('parents' in x.get('pending_fields',[]) for x in pending),'blockers':blocked}
            if not apply or blocked:return summary|{'elapsed_seconds':round(time.monotonic()-started,3)}
            if not pending and not state['layout']['missing_folders']:
                return summary|{'no_op':True,'elapsed_seconds':round(time.monotonic()-started,3),'publication':publish_current()}
            if any(x.get('recovery_required') for x in pending):raise SafetyError('UNRESOLVED_PRIOR_REQUEST_USE_RESUME_FOR_RECORDED_RUN')
            Path(config['backup_directory']).mkdir(parents=True,exist_ok=True,mode=0o700)
            run_dir=Path(config['run_directory'])/(section_key(section)+'-'+uuid.uuid4().hex[:12]);run_dir.mkdir(parents=True,mode=0o700)
            with journal.locked():
                with timed_stage(report,'Backup before plan preparation'):
                    journal.backup(Path(config['backup_directory'])/('before-destination-'+uuid.uuid4().hex))
                if capture_client._revision()!=capture['source_revision']:raise SafetyError('WORKBOOK_CHANGED_BEFORE_FREEZE_REFRESH_REQUIRED')
                with timed_stage(report,'Freeze plan and validate authority'):
                    prepared=freeze(config,section,journal,state,factory,run_dir,report)
            registry.setdefault('runs',[]).append(prepared);atomic_json(registry_path,registry)
        else:run_dir=Path(prepared['plan_path']).parent
        if not apply:return {'section':section,'preview_only':True,'saved_run_id':prepared['run_id'],'selected_identities':len(prepared['selected_source_ids'])}
        plan=plan_from_json(json.loads(Path(prepared['plan_path']).read_text()))
        if plan.proposal_hash!=prepared['plan_sha256']:raise SafetyError('SAVED_PLAN_HASH_MISMATCH')
        # Re-authorize only the exact stored plan after fresh resume mapping check.
        if resume or rollback:
            with journal.locked():
                saved=journal.read_evidence(prepared['source_metadata_ref'])['metadata']
                mutation_ids={op.file_id for op in plan.operations}
                authorize(journal,plan,{s:normalize(v,parent_context=s not in mutation_ids) for s,v in saved.items() if s in prepared['allowed_ids']},
                    prepared['instruction_ref'],section_id=prepared['section_id'])
        def client():return factory(set(prepared['allowed_ids']),set(prepared['selected_source_ids']),set(prepared['excluded_ids']),True)
        guard=RevisionGuard(capture_client,capture['source_revision'],report,config.get('revision_check_seconds',30))
        runner=DesiredRunner(journal,plan,client(),audit_permissions=False,progress=guard);guard.runner=runner
        previous={}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT,signal.SIGTERM):previous[sig]=signal.signal(sig,lambda *_:setattr(runner,'stop_requested',True))
        try:result=runner.run(apply=True,confirm_plan_sha256=plan.proposal_hash,source_ids=prepared['selected_source_ids'],
            limit=len(prepared['selected_source_ids']),workers=config.get('workers',10),transport_factory=client,rollback=bool(rollback))
        finally:
            for sig,handler in previous.items():signal.signal(sig,handler)
        with timed_stage(report,'Save result and status exports'), journal.locked():
            result_path=save_new(run_dir/('result-'+uuid.uuid4().hex+'.json'),result)
            exports=export_status(journal,result,run_dir/'exports')
            # DesiredRunner already made and fully verified its final snapshot.
            # Reuse it only if no event has changed since that backup.
            if runner.last_backup_path is None:raise SafetyError('FINAL_BACKUP_REQUIRED')
            backup=runner.last_backup_path
            tip=journal.records()[-1]
            if (runner.last_backup_manifest['head_hash']!=tip['hash']
                    or (backup/'events.jsonl').read_bytes()!=journal.path.read_bytes()):
                raise SafetyError('BACKUP_VERIFICATION_FAILED')
        finished={'section':section,'run_id':plan.run_id,'counts':result['counts'],'journal_sequence':tip['seq'],
            'backup':str(backup),'result_path':result_path,'exports':exports,'elapsed_seconds':round(time.monotonic()-started,3),
            'revision_stop_reason':guard.reason}
        report({'stage':'execution_finished',**finished})
        # Registry updates are private; failed publication never restarts actions.
        entry=next((x for x in registry['sections'] if x.get('proposal_path',x.get('publication_spec',{}).get('proposal_path'))==section),None)
        if entry is None:entry={};registry['sections'].append(entry)
        spec=copy.deepcopy(entry.get('publication_spec',entry))
        hashes=list(dict.fromkeys(spec.get('plan_sha256s',([spec['plan_sha256']] if spec.get('plan_sha256') else []))+[plan.proposal_hash]))
        paths=prepared['destination_paths'];created_ids={paths[p] for p in prepared['created_folder_keys']}
        prior_mode=spec.get('destination_mode') if spec.get('destination_id')==paths[section] else None
        spec.update({'section_id':prepared['section_id'],'title':section.rsplit('/',1)[-1],'proposal_path':section,'plan_sha256s':hashes,
            'plan_sha256':plan.proposal_hash,'source_ids':prepared['source_ids'],'destination_id':paths[section],
            'destination_mode':prior_mode or ('CREATED' if paths[section] in created_ids else ('REUSED_MOVED' if any(o.file_id==paths[section] and o.action=='MOVE' and o.before['parents']!=o.target['parents'] for o in plan.operations) else 'REUSED_EXISTING')),
            'destination_ids':[v for k,v in paths.items() if k==section or k.startswith(section+'/')],
            'destination_paths':{v:k for k,v in paths.items() if k==section or k.startswith(section+'/')},'retained_source_folder_ids':prepared['source_container_ids'],'source_folder_ids_left_in_place':prepared['source_container_ids']})
        entry.clear();entry.update(publication_spec=spec,destination_paths=paths);atomic_json(registry_path,registry)
        finished['publication']=publish_current()
        return finished



def verify_completed_sample(config,section,journal,source,factory,registry,limit):
    if type(limit) is not int or not 1<=limit<=30:raise SafetyError('COMPLETED_SAMPLE_LIMIT_1_TO_30')
    entry=next((e for e in registry['sections'] if e.get('publication_spec',e).get('proposal_path')==section),None)
    if entry is None:raise SafetyError('NO_COMPLETED_SECTION_REGISTRY')
    spec=entry.get('publication_spec',entry);paths=entry.get('destination_paths',{})
    h=build_history_index(journal);selected=sorted(spec['source_ids'])[:limit]
    records=select_records(source,section);groups={}
    for r in records:groups.setdefault(r['source_id'],{})[r['action']]=r
    excluded=set(config.get('excluded_ids',[]))|set(source['excluded_ids'])
    bad={r['source_id'] for r in source['records'].values() if r.get('withdrawn') or r.get('decision')=='No' or r['reference'] in source['excluded_refs']}
    if set(selected)&(excluded|bad):raise SafetyError('COMPLETED_SAMPLE_NOW_EXCLUDED')
    result=[];client=factory(set(selected)|set(paths.values()),set(),excluded,False)
    for sid in selected:
        actions=groups.get(sid)
        if not actions or sid not in h:raise SafetyError('COMPLETED_SOURCE_MISSING_FROM_FRESH_SECTION:'+sid)
        live=client.get_file(sid);rename=actions.get('RENAME');move=actions.get('MOVE')
        path=file_parent_path(move,rename,normalize(live))
        if path not in paths:raise SafetyError('COMPLETED_DESTINATION_MAPPING_CHANGED:'+path)
        target={'id':sid,'name':exact_rename(rename,normalize(live)) if rename else h[sid]['latest_observation']['name'],
                'parents':[paths[path]],'mime_type':live['mimeType']}
        result.append(reconcile_item(h[sid],target,live))
    return {'section':section,'preview_only':True,'coverage':'SAMPLE_NOT_SECTION_REVALIDATION','checked_files':len(selected),
            'counts':dict(Counter(x['disposition'] for x in result)),
            'blockers':[x for x in result if x['disposition']!='COMPLETE'],'journal_sequence':len(journal.records()),'writes':0}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination')
    parser.add_argument('--config',default='private/destination-config.json');parser.add_argument('--apply',action='store_true')
    parser.add_argument('--resume',action='store_true');parser.add_argument('--rollback',metavar='RUN_ID');parser.add_argument('--publish-only',metavar='DIRECTORY')
    parser.add_argument('--verify-completed',type=int,metavar='N',help='Read-only sample of 1–30 already completed files; never a whole-section completion claim')
    parser.add_argument('--json',action='store_true',help='Emit complete JSON records instead of readable console progress')
    args=parser.parse_args(argv);os.umask(0o077)
    from .destination_console import ConsoleReporter
    console=ConsoleReporter(json_output=args.json)
    try:
        config_path=Path(args.config).resolve();config=json.loads(config_path.read_text())
        console.open_log(config['run_directory'])
        result=run_destination(config,args.destination,apply=args.apply,resume=args.resume,rollback=args.rollback,publish_only=args.publish_only,verify_completed=args.verify_completed,
            report=console.progress)
        console.finish(result)
        return 2 if result.get('blockers') or result.get('revision_stop_reason') or any(k not in {'APPLIED','TARGET','COMPLETE','ROLLED_BACK','UNCHANGED'} and v for k,v in result.get('counts',{}).items()) and args.apply else 0
    except Exception as exc:
        from .direct_transport import TransportError
        console.error({'error':str(exc) if isinstance(exc,SafetyError) else type(exc).__name__,
            **({'request_error':exc.as_dict()} if isinstance(exc,TransportError) else {})});return 2
    finally:
        console.close()


if __name__=='__main__':raise SystemExit(main())
