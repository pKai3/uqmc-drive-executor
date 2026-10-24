"""Repeatable status-only publication; never calls a Drive mutation endpoint.

The authoritative journal stays unchanged. Publication receipts and preimages
are private files. A failed/unknown delivery is inspected before a later retry.
"""
from __future__ import annotations

import argparse
import copy
from collections import defaultdict
import hashlib
import json
import os
import re
from pathlib import Path

from .projection import HEADERS as LOG_HEADERS, validate_snapshot
from .sheets_capture import GoogleSheetsCapture

STATUS_HEADERS = ['Source ID','Open source','Move effect','Rename effect','Move latest event',
    'Rename latest event','Location effect','Verified exact name','Verified parent IDs',
    'Verified path','Move verified UTC','Rename verified UTC','Metadata verified UTC',
    'Path verified UTC','Operation IDs','Run IDs','Rollback operation IDs','Snapshot ID',
    'State reason codes']
EXTRA_HEADERS = ['Execution status','Execution snapshot ID','Verified destination']
BASE_WIDTHS = {'Proposed Moves':15,'Move Manifest':12,'Proposed Renames':18}

def col(n):
    result=''
    while n:
        n,r=divmod(n-1,26);result=chr(65+r)+result
    return result

def a1(title,row,column,height,width):
    return "'"+title.replace("'","''")+"'!"+col(column+1)+str(row+1)+':'+col(column+width)+str(row+height)

def pad(row,width):
    return (list(row)+['']*width)[:width]

def js(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))

def seal(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())

def read_existing(capture):
    api=capture.service.spreadsheets();wid=capture.workbook_id
    metadata=api.get(spreadsheetId=wid,fields='spreadsheetId,sheets(properties,tables)').execute(num_retries=0)
    properties={s['properties']['title']:s['properties'] for s in metadata['sheets']}
    required=set(BASE_WIDTHS)|{'Execution Status','Operation Log'}
    if not required<=properties.keys(): raise ValueError('PUBLICATION_TABS_MISSING')
    if 'Migration Summary' in properties:required.add('Migration Summary')
    ranges=[];names=[]
    for name in sorted(required):
        p=properties[name];width=BASE_WIDTHS.get(name,19 if name=='Execution Status' else 10 if name=='Migration Summary' else 26)
        if name in BASE_WIDTHS:width=min(width+3,p['gridProperties']['columnCount'])
        ranges.append(a1(name,0,0,p['gridProperties']['rowCount'],width));names.append(name)
    values=api.values().batchGet(spreadsheetId=wid,ranges=ranges,valueRenderOption='FORMULA').execute(num_retries=0)
    matrices={name:v.get('values',[]) for name,v in zip(names,values['valueRanges'])}
    return {'metadata':metadata,'properties':properties,'ranges':ranges,'names':names,'matrices':matrices}

def item_status(item):
    if not item:return 'NOT_TRACKED — no executor record'
    if {'POLICY_EXCLUDED','USER_EXCLUDED_SHORTCUT'} <= set(item.get('reason_codes',[])):
        return 'EXCLUDED — shortcut policy; historical effects retained'
    if {'RETIRED_EMPTY_FOLDER','VERIFIED_TRASHED'} <= set(item.get('reason_codes',[])):
        return 'RETIRED — verified empty obsolete folder; moved to Trash'
    if {'RETIRED','USER_CONFIRMED_DELETED','NOT_APPLIED'} <= set(item.get('reason_codes',[])):
        return 'RETIRED — user-confirmed deleted; not migrated'
    if {'EXCLUDED','MISSING_OR_INACCESSIBLE','NOT_APPLIED'} <= set(item.get('reason_codes',[])):
        return 'EXCLUDED — missing or inaccessible; not migrated'
    move,rename=item['move']['effect'],item['rename']['effect']
    location=item['location']['effect']
    suffix='; relocated with ancestor folder' if location in ('INHERITED','DIRECT_AND_INHERITED') else ''
    return f'Rename {rename}; Move {move}'+suffix

def build_sheet_plan(bundle,existing):
    snapshot=bundle['workbook'];validate_snapshot(snapshot)
    if bundle['operation_log']['headers']!=LOG_HEADERS:raise ValueError('EXPORT_LOG_HEADERS_MISMATCH')
    matrices=copy.deepcopy(existing['matrices']);props=copy.deepcopy(existing['properties'])
    if pad(matrices['Execution Status'][5],19)!=STATUS_HEADERS:raise ValueError('EXECUTION_HEADERS_CHANGED')
    if pad(matrices['Operation Log'][0],26)!=LOG_HEADERS:raise ValueError('LOG_HEADERS_CHANGED')
    items={i['source_id']:i for i in snapshot['items']}
    tracked={sid:i for sid,i in items.items() if i['observed']['read_state']!='NEVER_VERIFIED'
             or any(i[field]['operation_ids'] for field in ('move','rename')) or {'EXCLUDED','RETIRED','POLICY_EXCLUDED','RETIRED_EMPTY_FOLDER'} & set(i.get('reason_codes',[]))}
    operations={op['operation_id']:op for op in snapshot['operations']}
    requests=[];changes=[];sheet_sizes={n:dict(p['gridProperties']) for n,p in props.items()}

    def write(name,row,column,values):
        if not values:return
        width=len(values[0]);assert all(len(v)==width for v in values)
        old=matrices.get(name,[])
        if all(pad(old[row+n] if row+n<len(old) else [],column+width)[column:]==v for n,v in enumerate(values)):return
        sizes=sheet_sizes[name];newrows=max(sizes['rowCount'],row+len(values));newcols=max(sizes['columnCount'],column+width)
        if newrows!=sizes['rowCount'] or newcols!=sizes['columnCount']:
            requests.append({'updateSheetProperties':{'properties':{'sheetId':props[name]['sheetId'],
                'gridProperties':{'rowCount':newrows,'columnCount':newcols}},'fields':'gridProperties(rowCount,columnCount)'}})
            sizes.update(rowCount=newrows,columnCount=newcols)
        requests.append({'updateCells':{'range':{'sheetId':props[name]['sheetId'],'startRowIndex':row,
            'endRowIndex':row+len(values),'startColumnIndex':column,'endColumnIndex':column+width},
            'rows':[{'values':[{} if v=='' or v is None else {'userEnteredValue':
                {'numberValue':v} if isinstance(v,(int,float)) else {'stringValue':str(v)}} for v in line]} for line in values],
            'fields':'userEnteredValue'}})
        changes.append({'range':a1(name,row,column,len(values),width),'values':values})

    source_rows={}
    for n,row in enumerate(matrices['Execution Status'][6:],6):
        if row and row[0]:
            if row[0] in source_rows:raise ValueError('DUPLICATE_STATUS_SOURCE')
            source_rows[row[0]]=n
    missing=set(tracked)-source_rows.keys()
    # Newly registered identities need a status row too. Require both the
    # validated publication snapshot and a current plan's stable-ID binding;
    # never append arbitrary journal identities or infer IDs from display names.
    plan_ids={pad(row,width)[column]
        for name,width,column in (('Proposed Renames',18,16),('Move Manifest',12,11))
        for row in matrices[name][1:] if pad(row,width)[column]}
    if missing-plan_ids:
        raise ValueError('TRACKED_SOURCE_NOT_IN_CURRENT_PLAN:'+str(len(missing-plan_ids)))
    if any(not isinstance(sid,str) or sid=='root' or not re.fullmatch(r'[A-Za-z0-9_-]+',sid)
           for sid in missing):
        raise ValueError('INVALID_STATUS_SOURCE_ID')
    added_status_source_ids=sorted(missing)
    next_row=max(6,len(matrices['Execution Status']))
    exemplar=max(source_rows.values(),default=None)
    for sid in added_status_source_ids:
        source_rows[sid]=next_row
        write('Execution Status',next_row,0,[[sid,'https://drive.google.com/file/d/'+sid+'/view']])
        if exemplar is not None:
            sheet_id=props['Execution Status']['sheetId']
            requests.append({'copyPaste':{
                'source':{'sheetId':sheet_id,'startRowIndex':exemplar,'endRowIndex':exemplar+1,
                          'startColumnIndex':0,'endColumnIndex':19},
                'destination':{'sheetId':sheet_id,'startRowIndex':next_row,'endRowIndex':next_row+1,
                               'startColumnIndex':0,'endColumnIndex':19},
                'pasteType':'PASTE_FORMAT'}})
        next_row+=1
    for sid,item in tracked.items():
        m,r,l,o=item['move'],item['rename'],item['location'],item['observed']
        opids=list(dict.fromkeys(m['operation_ids']+r['operation_ids']))
        runids=sorted({operations[op]['run_id'] for op in opids if op in operations})
        rollbacks=[op for op in opids if operations.get(op,{}).get('direction')=='ROLLBACK']
        write('Execution Status',source_rows[sid],2,[[m['effect'],r['effect'],m['last_event_state'],r['last_event_state'],
            l['effect'],o['exact_name'],None if o['parent_ids'] is None else js(o['parent_ids']),l['verified_path'],
            m['verified_at_utc'],r['verified_at_utc'],o['metadata_verified_at_utc'],l['path_verified_at_utc'],
            js(opids),js(runids),js(rollbacks),snapshot['snapshot_id'],','.join(item['reason_codes'])]])

    existing_log=matrices['Operation Log'][1:];seen={}
    for row in existing_log:
        if row and row[0]:
            if row[0] in seen:raise ValueError('DUPLICATE_REMOTE_LOG_EVENT')
            seen[row[0]]=pad(row,26)
    incoming=bundle['operation_log']['rows'];append=[];incoming_ids=set()
    for row in incoming:
        if len(row)!=26 or row[0] in incoming_ids:raise ValueError('INVALID_EXPORTED_LOG')
        incoming_ids.add(row[0])
        if row[0] not in seen:append.append(row)
        # Existing rows are immutable history. Never rewrite a delivered event,
        # including a historical projection whose formatting has since improved.
    write('Operation Log',len(existing_log)+1,0,append)

    section_data=bundle.get('sections',{})
    sections=section_data if isinstance(section_data,list) else section_data.get('sections',[])
    if sections:
        name='Migration Summary'
        summary_headers=['Destination section','Execution status','Verified files','Expected files',
            'Destination treatment','Actual destination','Created folders','Source folders retained','Snapshot ID','Verified UTC']
        if name not in props:
            sid=max(p['sheetId'] for p in props.values())+1
            props[name]={'sheetId':sid,'title':name,'gridProperties':{'rowCount':max(100,len(sections)+2),'columnCount':10}}
            sheet_sizes[name]=dict(props[name]['gridProperties'])
            requests.append({'addSheet':{'properties':dict(props[name],gridProperties=dict(props[name]['gridProperties'],frozenRowCount=1))}})
            matrices[name]=[]
        elif matrices[name] and pad(matrices[name][0],10)!=summary_headers:
            raise ValueError('MIGRATION_SUMMARY_OCCUPIED')
        summary_rows=[summary_headers]
        for s in sections:
            summary_rows.append([s.get('path',s.get('title','')),s.get('status',s.get('state','UNKNOWN')),
                s.get('completed_file_count',0),s.get('expected_file_count',0),s.get('mode',''),
                s.get('destination_url',''),s.get('created_folder_count',0),len(s.get('retained_source_folder_ids',[])),
                snapshot['snapshot_id'],s.get('verified_at_utc',snapshot['generated_at_utc'])])
        write(name,0,0,summary_rows)
        sid=props[name]['sheetId']
        requests.extend([{'repeatCell':{'range':{'sheetId':sid,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':0,'endColumnIndex':10},
            'cell':{'userEnteredFormat':{'backgroundColor':{'red':0.137,'green':0.337,'blue':0.239},'textFormat':{'bold':True,'foregroundColor':{'red':1,'green':1,'blue':1}},'wrapStrategy':'WRAP'}},'fields':'userEnteredFormat'}},
            {'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'COLUMNS','startIndex':0,'endIndex':10},'properties':{'pixelSize':220},'fields':'pixelSize'}},
            {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':1,'endRowIndex':len(summary_rows),'startColumnIndex':0,'endColumnIndex':10},'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP','verticalAlignment':'TOP'}},'fields':'userEnteredFormat'}}])
    retained={sid for s in sections for sid in s.get('retained_source_folder_ids',[])}
    destination_urls={}
    for s in sections:
        for sid in s.get('source_ids',s.get('expected_source_ids',s.get('expected_file_ids',[]))):
            destination_urls[sid]=s.get('destination_url',s.get('destination_link',''))
    def destination(item):
        if not item or item['observed']['read_state']!='VERIFIED':return ''
        parents=item['observed']['parent_ids']
        return 'https://drive.google.com/drive/folders/'+parents[0] if parents and len(parents)==1 else ''
    retired = {sid for sid, item in items.items() if
        {'RETIRED','USER_CONFIRMED_DELETED','NOT_APPLIED'} <= set(item.get('reason_codes',[]))}
    policy_excluded = {sid for sid,item in items.items() if
        {"POLICY_EXCLUDED","USER_EXCLUDED_SHORTCUT"} <= set(item.get("reason_codes",[]))}
    proposal_members=defaultdict(set)
    for row in matrices['Move Manifest'][1:]:
        row=pad(row,12)
        if row[9] and row[11]:proposal_members[row[9]].add(row[11])
    for name,base in BASE_WIDTHS.items():
        matrix=matrices[name]
        if any(v not in ('',EXTRA_HEADERS[n]) for n,v in enumerate(pad(matrix[0],base+3)[base:])):
            raise ValueError('PUBLICATION_COLUMNS_OCCUPIED:'+name)
        for column in range(base,base+3):
            if pad(matrix[0],base+3)[column]=='':
                if any(pad(r,base+3)[column]!='' for r in matrix[1:]):
                    raise ValueError('PUBLICATION_COLUMN_BODY_OCCUPIED:'+name)
        rows=[EXTRA_HEADERS]
        for row in matrix[1:]:
            row=pad(row,base)
            if not any(row):rows.append(['','','']);continue
            if name=='Proposed Moves':
                all_members=proposal_members.get(row[0],set())
                retired_count=len(all_members & retired)
                excluded_count=len(all_members & policy_excluded)
                members=all_members-retired-policy_excluded;known=[items[s] for s in members if s in tracked]
                applied=sum(i['move']['effect']=='APPLIED' or i['location']['effect'] in ('INHERITED','DIRECT_AND_INHERITED') for i in known)
                kept=len(members&retained)
                text=('NOT_TRACKED — no executor record' if not known else
                    f'{"COMPLETED" if applied+kept==len(members) else "PARTIAL"}: {applied}/{len(members)} assigned identities relocated')
                if kept:text+=f'; {kept} source folders retained'
                if retired_count:text+=f'; {retired_count} retired (not migrated)'
                if excluded_count:text+=f'; {excluded_count} shortcuts excluded (history retained)'
                links=sorted({destination(i) for i in known if destination(i)})
                link=links[0] if len(links)==1 else ''
            else:
                sid=row[16 if name=='Proposed Renames' else 11];item=tracked.get(sid)
                text='SOURCE FOLDER RETAINED; destination created' if sid in retained else item_status(item)
                if item and name=='Proposed Renames' and item['rename']['effect']=='APPLIED':
                    proposed=row[2]
                    observed=item['observed']['exact_name']
                    # Native display suffixes are not real Google titles.
                    native=item['observed']['mime_type'] in ('application/vnd.google-apps.document','application/vnd.google-apps.spreadsheet','application/vnd.google-apps.presentation')
                    if proposed!=observed and not (native and proposed in {str(observed)+e for e in ('.gdoc','.gsheet','.gslides')}):
                        text+='; current proposal differs from verified name'
                link=destination(item)
            rows.append([text,snapshot['snapshot_id'],link])
        write(name,0,base,rows)
        sid=props[name]['sheetId']
        requests.append({'copyPaste':{'source':{'sheetId':sid,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':base-1,'endColumnIndex':base},
            'destination':{'sheetId':sid,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':base,'endColumnIndex':base+3},'pasteType':'PASTE_FORMAT'}})
        requests.append({'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'COLUMNS','startIndex':base,'endIndex':base+3},'properties':{'pixelSize':300},'fields':'pixelSize'}})
        requests.append({'repeatCell':{'range':{'sheetId':sid,'startRowIndex':1,'endRowIndex':len(rows),'startColumnIndex':base,'endColumnIndex':base+3},'cell':{'userEnteredFormat':{'wrapStrategy':'CLIP','verticalAlignment':'TOP'}},'fields':'userEnteredFormat(wrapStrategy,verticalAlignment)'}})
    complete=sum(s.get('state',s.get('status'))=='COMPLETED' for s in sections)
    renames=sum(i['rename']['effect']=='APPLIED' for i in tracked.values())
    moves=sum(i['move']['effect']=='APPLIED' for i in tracked.values())
    write('Execution Status',0,2,[[f'{complete} completed destination sections — {renames} verified renames; {moves} direct moves'],
        [f'STATIC CHECKPOINT {snapshot["journal_sequence"]}, not live Drive. Verified outcomes from the local journal. NOT_TRACKED is not proof of original location. Proposal edits and approvals are preserved.'],
        [snapshot['snapshot_id']],[snapshot['generated_at_utc']],
        [f'{snapshot["coverage"]["identity_count"]:,} source identities; {len(tracked)} tracked identities updated. Historical failures remain in the log; latest verified item effects are shown below. No global live reconciliation. Other rows retain their prior snapshot IDs.']])
    return {'snapshot_id':snapshot['snapshot_id'],'journal_sequence':snapshot['journal_sequence'],
        'requests':requests,'changes':changes,'tracked_rows':len(tracked),'new_log_rows':len(append),
        'added_status_source_ids':added_status_source_ids,
        'verified_renames':renames,'verified_direct_moves':moves,
        'existing_digest':seal(existing),'preserved_digest':seal({n:[pad(r,w) for r in matrices[n]] for n,w in BASE_WIDTHS.items()})}

def publish(capture,bundle,output,*,apply=False):
    output=Path(output);before=read_existing(capture);plan=build_sheet_plan(bundle,before)
    save(output/'prewrite.json',{'before':before,'plan':plan})
    summary={k:v for k,v in plan.items() if k not in ('requests','changes')}
    if not apply:return dict(summary,result='PREVIEW',ranges=[c['range'] for c in plan['changes']])
    # Re-read every affected existing cell/identifier just before one atomic batch.
    # This narrows the race; Sheets does not provide compare-and-swap here.
    current=read_existing(capture)
    if seal(current)!=plan['existing_digest']:raise ValueError('WORKBOOK_CHANGED_BEFORE_PUBLICATION')
    api=capture.service.spreadsheets();wid=capture.workbook_id
    save(output/'intent.json',dict(summary,request_sha256=seal(plan['requests']),state='ATTEMPTED'))
    error=None
    try:api.batchUpdate(spreadsheetId=wid,body={'requests':plan['requests']}).execute(num_retries=0)
    except Exception as exc:error=type(exc).__name__ # Never expose credential-bearing raw errors.
    # No blind replay: inspect all changed cells even after a timeout.
    mismatches=[]
    for start in range(0,len(plan['changes']),100):
        chunk=plan['changes'][start:start+100]
        observed=api.values().batchGet(spreadsheetId=wid,ranges=[c['range'] for c in chunk],valueRenderOption='FORMULA').execute(num_retries=0)
        if len(observed.get('valueRanges',[]))!=len(chunk):
            raise ValueError('INCOMPLETE_PUBLICATION_READBACK')
        for change,result in zip(chunk,observed['valueRanges']):
            rows=result.get('values',[]);expected=change['values'];width=len(expected[0])
            actual=[pad(rows[n] if n<len(rows) else [],width) for n in range(len(expected))]
            wanted=[[v if v is not None else '' for v in row] for row in expected]
            if actual!=wanted:mismatches.append(change['range'])
    after=read_existing(capture)
    preserved=seal({n:[pad(r,w) for r in after['matrices'][n]] for n,w in BASE_WIDTHS.items()})==plan['preserved_digest']
    result=dict(summary,result='PUBLICATION_VERIFIED' if not mismatches and preserved else 'READBACK_CONFLICT',
        ranges=[c['range'] for c in plan['changes']],readback_mismatches=mismatches,
        proposal_values_and_formulas_preserved=preserved,write_error_class=error)
    save(output/'result.json',result)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',required=True);parser.add_argument('--workbook-id',required=True)
    parser.add_argument('--token',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    capture=GoogleSheetsCapture.from_authorized_user_file(args.token,workbook_id=args.workbook_id)
    result=publish(capture,json.loads(Path(args.bundle).read_text()),args.output,apply=args.apply)
    print(json.dumps({k:v for k,v in result.items() if k!='ranges'},ensure_ascii=False),flush=True)
    if result['result']=='READBACK_CONFLICT':raise SystemExit(2)

if __name__=='__main__':main()
