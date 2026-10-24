"""Offline status-publisher tests. No network, secrets or real source data."""
import copy
import re
import tempfile
import unittest
from types import SimpleNamespace

from test_projection import MemoryJournal
from uqmc_recovery.importer import HEADERS as PROPOSAL_HEADERS
from uqmc_recovery.projection import HEADERS, build_projections
from uqmc_recovery.sheet_publication import (
    BASE_WIDTHS, STATUS_HEADERS, EXTRA_HEADERS, build_sheet_plan, read_existing,
    publish, pad,
)


def fixture_bundle():
    journal=MemoryJournal()
    plan=journal.evidence({'operations':[{'operation_id':'rename-1'}]})
    journal.append('DRAFTED',payload={'batch_id':'fixture-batch','plan_ref':plan})
    before=journal.evidence({'id':'source-one','name':'Old.txt','mime_type':'text/plain','parents':['parent-one']})
    after=journal.evidence({'id':'source-one','name':'=KEEP_AS_LITERAL()','mime_type':'text/plain','parents':['parent-one']})
    journal.append('VERIFIED_APPLIED','rename-1',1,{'batch_id':'fixture-batch',
        'proposal_hash':'a'*64,'action':'RENAME','direction':'FORWARD','file_id':'source-one',
        'depends_on':[],'proposal_refs':['RENAME-1'],'actor':'fixture',
        'before_ref':before,'intended_ref':after,'observed_ref':after,'reason_code':'NONE'})
    result=build_projections(journal,allowed_source_ids=['source-one','untracked'])
    result['sections']={'sections':[]}
    return result


class Request:
    def __init__(self,fn):self.fn=fn
    def execute(self,num_retries=0):
        assert num_retries==0
        return self.fn()


def column_index(value):
    result=0
    for char in value:result=result*26+ord(char)-64
    return result-1


class FakeSheets:
    def __init__(self):
        self.matrices={name:[copy.deepcopy(PROPOSAL_HEADERS[name])] for name in BASE_WIDTHS}
        move=['MOVE-1','old folder','','target','','',1,'','','','Yes','Human status','','','']
        manifest=['Old.txt','target/Old.txt','Single item',1,'','','','','','MOVE-1','ITEM-1','source-one']
        rename=['2 Useful','Old.txt','=KEEP_AS_LITERAL()','','Yes','=IF(E2="Yes","Approved","")',
            'target','','','','','','','RENAME-1','MOVE-1','ITEM-1','source-one','']
        self.matrices['Proposed Moves'].append(move)
        self.matrices['Move Manifest'].append(manifest)
        self.matrices['Proposed Renames'].append(rename)
        self.matrices['Execution Status']=[['Execution Status'],[],[],[],[],STATUS_HEADERS,
            ['untracked','=HYPERLINK("https://example.test/untracked","Open")','NOT_TRACKED','NOT_TRACKED'],
            ['source-one','=HYPERLINK("https://example.test/source","Open")','NOT_TRACKED','NOT_TRACKED']]
        self.matrices['Operation Log']=[HEADERS]
        self.properties={name:{'sheetId':i+100,'title':name,'gridProperties':{
            'rowCount':50,'columnCount':BASE_WIDTHS.get(name,19 if name=='Execution Status' else 26)}}
            for i,name in enumerate(self.matrices)}
        self.updates=[];self.timeout_after_update=False;self.drop_verification_range=False
        self.mutate_on_metadata=None;self.metadata_reads=0

    def spreadsheets(self):return self
    def values(self):return self
    def get(self,**kwargs):
        def run():
            self.metadata_reads+=1
            if self.mutate_on_metadata==self.metadata_reads:
                self.matrices['Proposed Moves'][1][10]='No'
            return {'spreadsheetId':'fixture-workbook','sheets':[{'properties':copy.deepcopy(p)} for p in self.properties.values()]}
        return Request(run)

    def batchGet(self,*,ranges,**kwargs):
        def run():
            result=[]
            for address in ranges:
                match=re.fullmatch(r"'(.+)'!([A-Z]+)(\d+):([A-Z]+)(\d+)",address)
                name,c0,r0,c1,r1=match.groups();c0=column_index(c0);c1=column_index(c1)+1
                rows=[]
                for row in self.matrices[name][int(r0)-1:int(r1)]:
                    values=copy.deepcopy(row[c0:c1])
                    while values and values[-1]=='':values.pop()
                    rows.append(values)
                while rows and not rows[-1]:rows.pop()
                result.append({'range':address,'values':rows})
            if self.drop_verification_range and self.updates and len(ranges)>5:result=result[:-1]
            return {'valueRanges':result}
        return Request(run)

    def batchUpdate(self,*,body,**kwargs):
        def run():
            self.updates.append(copy.deepcopy(body))
            by_id={p['sheetId']:name for name,p in self.properties.items()}
            for request in body['requests']:
                if 'updateSheetProperties' in request:
                    p=request['updateSheetProperties']['properties'];name=by_id[p['sheetId']]
                    self.properties[name]['gridProperties'].update(p['gridProperties'])
                if 'updateCells' not in request:continue
                update=request['updateCells'];rng=update['range'];name=by_id[rng['sheetId']]
                matrix=self.matrices[name]
                while len(matrix)<rng['endRowIndex']:matrix.append([])
                for offset,row in enumerate(update['rows']):
                    n=rng['startRowIndex']+offset;matrix[n]=pad(matrix[n],rng['endColumnIndex'])
                    for c,cell in enumerate(row['values'],rng['startColumnIndex']):
                        value=cell.get('userEnteredValue',{})
                        matrix[n][c]=next(iter(value.values())) if value else ''
            if self.timeout_after_update:raise TimeoutError('simulated response loss')
            return {'replies':[]}
        return Request(run)


class SheetPublicationTests(unittest.TestCase):
    def setUp(self):
        self.api=FakeSheets()
        self.capture=SimpleNamespace(service=self.api,workbook_id='fixture-workbook')
        self.bundle=fixture_bundle()

    def plan(self):return build_sheet_plan(self.bundle,read_existing(self.capture))

    def test_stable_id_join_and_literal_formula_name(self):
        plan=self.plan()
        ranges=[change['range'] for change in plan['changes']]
        self.assertIn("'Execution Status'!C8:S8",ranges)
        self.assertNotIn("'Execution Status'!C7:S7",ranges)
        status=next(r['updateCells'] for r in plan['requests'] if r.get('updateCells',{}).get('range',{}).get('startRowIndex')==7)
        self.assertEqual(status['rows'][0]['values'][5]['userEnteredValue'],{'stringValue':'=KEEP_AS_LITERAL()'})

    def test_new_registered_plan_identity_gets_status_row_without_changing_existing_links(self):
        self.api.matrices['Execution Status'].pop()
        self.api.properties['Execution Status']['gridProperties']['rowCount']=7
        before=copy.deepcopy(self.api.matrices['Execution Status'][6])
        with tempfile.TemporaryDirectory() as tmp:
            result=publish(self.capture,self.bundle,tmp,apply=True)
        self.assertEqual(result['result'],'PUBLICATION_VERIFIED')
        self.assertEqual(result['added_status_source_ids'],['source-one'])
        self.assertEqual(self.api.matrices['Execution Status'][6],before)
        row=self.api.matrices['Execution Status'][7]
        self.assertEqual(row[:2],['source-one','https://drive.google.com/file/d/source-one/view'])
        self.assertEqual(row[3],'APPLIED')
        self.assertEqual(row[7],'=KEEP_AS_LITERAL()')
        self.assertEqual(self.api.properties['Execution Status']['gridProperties']['rowCount'],8)
        formatting=[r['copyPaste'] for r in self.api.updates[0]['requests']
                    if r.get('copyPaste',{}).get('destination',{}).get('sheetId')==self.api.properties['Execution Status']['sheetId']]
        self.assertEqual(len(formatting),1)
        self.assertEqual(formatting[0]['pasteType'],'PASTE_FORMAT')
        self.assertTrue(result['proposal_values_and_formulas_preserved'])

    def test_missing_status_identity_requires_current_plan_binding(self):
        self.api.matrices['Execution Status'].pop()
        self.api.matrices['Proposed Renames']=self.api.matrices['Proposed Renames'][:1]
        self.api.matrices['Move Manifest']=self.api.matrices['Move Manifest'][:1]
        with self.assertRaisesRegex(ValueError,'TRACKED_SOURCE_NOT_IN_CURRENT_PLAN:1'):
            self.plan()
        self.assertEqual(self.api.updates,[])

    def test_missing_status_identity_can_match_manifest_without_rename(self):
        self.api.matrices['Execution Status'].pop()
        self.api.matrices['Proposed Renames']=self.api.matrices['Proposed Renames'][:1]
        self.assertEqual(self.plan()['added_status_source_ids'],['source-one'])

    def test_lost_response_and_retry_do_not_duplicate_appended_status(self):
        self.api.matrices['Execution Status'].pop()
        self.api.timeout_after_update=True
        with tempfile.TemporaryDirectory() as tmp:
            first=publish(self.capture,self.bundle,tmp,apply=True)
            second=publish(self.capture,self.bundle,tmp,apply=True)
        self.assertEqual(first['result'],'PUBLICATION_VERIFIED')
        self.assertEqual(first['write_error_class'],'TimeoutError')
        self.assertEqual(second['added_status_source_ids'],[])
        self.assertEqual(sum(row[0]=='source-one' for row in self.api.matrices['Execution Status'][6:]),1)
        self.assertEqual(len(self.api.matrices['Operation Log']),2)

    def test_existing_log_event_is_not_rewritten_or_duplicated(self):
        existing=copy.deepcopy(self.bundle['operation_log']['rows'][0]);existing[24]='Historical text'
        self.api.matrices['Operation Log'].append(existing)
        plan=self.plan()
        self.assertEqual(plan['new_log_rows'],0)
        self.assertFalse(any(c['range'].startswith("'Operation Log'!") for c in plan['changes']))

    def test_all_original_proposal_cells_are_outside_write_ranges(self):
        plan=self.plan();by_id={p['sheetId']:name for name,p in self.api.properties.items()}
        for request in plan['requests']:
            update=request.get('updateCells')
            if update:
                rng=update['range'];name=by_id[rng['sheetId']]
                if name in BASE_WIDTHS:self.assertGreaterEqual(rng['startColumnIndex'],BASE_WIDTHS[name])

    def test_occupied_header_blocks(self):
        self.api.matrices['Proposed Moves'][0].append('Human notes')
        self.api.properties['Proposed Moves']['gridProperties']['columnCount']=18
        with self.assertRaisesRegex(ValueError,'PUBLICATION_COLUMNS_OCCUPIED'):self.plan()

    def test_blank_header_does_not_authorize_overwriting_body_notes(self):
        self.api.matrices['Proposed Moves'][1].append('User note below blank header')
        self.api.properties['Proposed Moves']['gridProperties']['columnCount']=18
        with self.assertRaisesRegex(ValueError,'PUBLICATION_COLUMN_BODY_OCCUPIED'):self.plan()

    def test_duplicate_source_join_blocks(self):
        self.api.matrices['Execution Status'].append(['source-one'])
        with self.assertRaisesRegex(ValueError,'DUPLICATE_STATUS_SOURCE'):self.plan()

    def test_concurrent_approval_edit_stops_before_write(self):
        self.api.mutate_on_metadata=2
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'WORKBOOK_CHANGED_BEFORE_PUBLICATION'):
                publish(self.capture,self.bundle,tmp,apply=True)
        self.assertEqual(self.api.updates,[])
        self.assertEqual(self.api.matrices['Proposed Moves'][1][10],'No')

    def test_response_lost_after_success_is_verified_without_replay(self):
        self.api.timeout_after_update=True
        preserved={name:copy.deepcopy(rows) for name,rows in self.api.matrices.items() if name in BASE_WIDTHS}
        with tempfile.TemporaryDirectory() as tmp:result=publish(self.capture,self.bundle,tmp,apply=True)
        self.assertEqual(result['result'],'PUBLICATION_VERIFIED')
        self.assertEqual(result['write_error_class'],'TimeoutError')
        self.assertEqual(len(self.api.updates),1)
        for name,rows in preserved.items():
            self.assertEqual([pad(row,BASE_WIDTHS[name]) for row in self.api.matrices[name]],rows)

    def test_incomplete_readback_cannot_be_success(self):
        self.api.drop_verification_range=True
        with tempfile.TemporaryDirectory() as tmp:
            try:result=publish(self.capture,self.bundle,tmp,apply=True)
            except (ValueError,RuntimeError):return
        self.assertNotEqual(result['result'],'PUBLICATION_VERIFIED')

    def test_retry_after_known_delivery_does_not_duplicate_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            first=publish(self.capture,self.bundle,tmp,apply=True)
            second=publish(self.capture,self.bundle,tmp,apply=True)
        self.assertEqual(first['new_log_rows'],1)
        self.assertEqual(second['new_log_rows'],0)
        self.assertEqual(len(self.api.matrices['Operation Log']),2)


if __name__=='__main__':unittest.main()
