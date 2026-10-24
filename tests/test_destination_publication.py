"""Publication integration using only synthetic local evidence and fake APIs."""
import base64
import copy
import gzip
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_projection import MemoryJournal
from test_sheet_publication import FakeSheets, Request
from uqmc_recovery import destination_publication as p
from uqmc_recovery import viewer_publication as v


def fixture():
    j=MemoryJournal();ph='a'*64
    before={'id':'source-one','name':'Old.txt','parents':['old-parent'],'mime_type':'text/plain'}
    target={**before,'name':'New.txt','parents':['destination']}
    ops=[{'operation_id':'rename','file_id':'source-one','action':'RENAME','before':before,'target':{'name':'New.txt'},'depends_on':[]},
         {'operation_id':'move','file_id':'source-one','action':'MOVE','before':{**before,'name':'New.txt'},'target':{'parents':['destination']},'depends_on':['rename']}]
    j.append('DRAFTED',payload={'proposal_hash':ph,'run_id':'run','plan_ref':j.evidence({'operations':ops})})
    j.append('DESIRED_APPLIED','desired-fixture',1,{'run_id':'run','plan_sha256':ph,'source_id':'source-one',
        'direction':'FORWARD','operation_order':1,'dependencies':[],'original_operation_ids':['rename','move'],
        'before_ref':j.evidence(before),'target_ref':j.evidence(target),'observed_ref':j.evidence(target),
        'args':{'name':'New.txt','addParents':'destination','removeParents':'old-parent'},'audit_permissions':False})
    raw={'id':'destination','name':'Section','parents':['anchor'],'mime_type':'application/vnd.google-apps.folder','trashed':False}
    j.append('DESTINATION_VERIFIED',payload={'section_id':'section','file_id':'destination','exact_name':'Section',
        'parent_ids':['anchor'],'proposal_path':'Root/Section','observed_ref':j.evidence(raw)})
    spec={'section_id':'section','title':'Section','proposal_path':'Root/Section','plan_sha256':ph,
        'source_ids':['source-one'],'destination_id':'destination','destination_mode':'REUSED_EXISTING'}
    def node(i,parent,name,folder,sid='',section='destination'):
        return dict(i=i,p=parent,n=name,o=None,f=folder,id=sid,s=section,c=[],changed=False,review=False)
    data={'nodes':[node(0,-1,'Root',True),node(1,0,'Section',True),node(2,1,'New.txt',False,'source-one'),
                   node(3,-1,'Untracked',False,'untracked','retained')],
          'roots':{'destination':[0],'retained':[3]},'notes':[],'stats':{}}
    packed=base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()
    html=('<html><style></style><script id="tree-data">'+packed+'</script><section class="execution-summary"></section></html>').encode()
    sheet=FakeSheets()
    sheet.matrices['Proposed Renames'][1][2]='New.txt'
    sheet.matrices['Proposed Renames'][1][6]='Root/Section'
    sheet.matrices['Migration Summary']=[['Destination section','Execution status','Verified files','Expected files',
        'Destination treatment','Actual destination','Created folders','Source folders retained','Snapshot ID','Verified UTC']]
    sheet.properties['Migration Summary']={'sheetId':200,'title':'Migration Summary','gridProperties':{'rowCount':100,'columnCount':10}}
    workbook={'exported_at_utc':'2026-01-01T00:00:00Z','sheets':copy.deepcopy(sheet.matrices)}
    return j,{'sections':[{'publication_spec':spec}]},html,sheet,workbook


class Crash(BaseException):pass


class FakeDrive:
    def __init__(self,content):
        self.content=content;self.writes=[];self.fail=None;self.fail_read=False
        self.meta={'id':'viewer','name':'Original.html','parents':['reports'],'mimeType':'text/html','trashed':False,'version':'1'}
    def metadata(self,fid):
        assert fid=='viewer'
        if self.fail_read:raise TimeoutError('PRIVATE_SECRET_NOT_FOR_EXPORT')
        return copy.deepcopy(self.meta)
    def download(self,fid):return self.content
    def replace_content(self,fid,content):
        assert fid=='viewer';self.writes.append(content);self.content=content;self.meta['version']='2'
        if self.fail=='timeout':raise TimeoutError('PRIVATE_SECRET_NOT_FOR_EXPORT')
        if self.fail=='crash':raise Crash()
        if self.fail=='readback':self.fail_read=True


class DestinationPublicationTests(unittest.TestCase):
    def setUp(self):
        sleeper=patch.object(p,'sleep');self.sleep=sleeper.start();self.addCleanup(sleeper.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.j,self.registry,html,self.sheet,self.workbook=fixture()
        self.drive=FakeDrive(html)
        self.capture=SimpleNamespace(service=self.sheet,workbook_id='fixture-workbook')
        self.config={'publication_directory':self.tmp.name,'identity_basis':{'identity_basis_version':1,'source_ids':['source-one','untracked']},
                     'workbook_id':'fixture-workbook','viewer_file_id':'viewer'}
    def freeze(self):
        return p.freeze_publication(self.config,self.registry,journal=self.j,fresh_workbook=self.workbook,drive=self.drive)
    def run_publish(self,path=None):
        return p.resume_publication(path or self.freeze(),sheet_capture=self.capture,drive=self.drive,apply=True)
    def test_freeze_preview_no_writes_and_private_output(self):
        before=copy.deepcopy(self.j.events)
        result=p.publish_registered_sections(self.config,self.registry,journal=self.j,fresh_workbook=self.workbook,
            sheet_capture=self.capture,drive=self.drive)
        self.assertEqual(result['result'],'PREVIEW');self.assertFalse(self.sheet.updates);self.assertFalse(self.drive.writes)
        self.assertEqual(before,self.j.events)
        self.assertEqual(Path(result['html_path']).stat().st_mode&0o777,0o600)
        html=Path(result['html_path']).read_text();data=v.unpack(html)
        self.assertEqual(data['execution_sections']['sections'][0]['status'],'COMPLETED')
        self.assertEqual(data['nodes'][1]['destination_overlay']['mode'],'REUSED_EXISTING')
        self.assertNotIn('observed_ref',json.dumps(data));self.assertNotIn('sha256:',json.dumps(data))
    def test_complete_sheet_and_same_html_preserve_metadata(self):
        identity={k:v for k,v in self.drive.meta.items() if k!='version'}
        result=self.run_publish()
        self.assertEqual(result['result'],'PUBLICATION_VERIFIED')
        self.assertEqual(identity,{k:v for k,v in self.drive.meta.items() if k!='version'})
        self.assertEqual(len(self.drive.writes),1);self.assertEqual(len(self.sheet.updates),1)
        self.assertEqual(self.sheet.matrices['Proposed Moves'][1][10],'Yes')
        self.assertEqual(self.sheet.matrices['Proposed Renames'][1][5],'=IF(E2="Yes","Approved","")')
    def test_sheet_only_recovery_appends_identity_and_preserves_other_delivery_receipts(self):
        path=self.freeze()
        receipt=p._load(path/'publication.json')
        receipt['sheet']={'state':'UNCERTAIN','reason':'TRACKED_SOURCE_NOT_IN_STATUS'}
        receipt['html']={'state':'VERIFIED','verified_at_utc':'2026-01-01T00:00:00Z'}
        receipt['server_sync']={'state':'VERIFIED','sha256':'existing-server-hash'}
        p._save(path/'publication.json',receipt)
        self.sheet.matrices['Execution Status'].pop()
        with patch('uqmc_recovery.server_sync.sync_publication',side_effect=AssertionError('server touched')):
            result=p.resume_publication(path,sheet_capture=self.capture,drive=None,
                apply=True,server_sync={'enabled':True},sheet_only=True)
        self.assertEqual(result['result'],'PUBLICATION_VERIFIED')
        self.assertEqual(result['sheet']['result']['added_status_source_ids'],['source-one'])
        self.assertEqual(result['html'],receipt['html'])
        self.assertEqual(result['server_sync'],receipt['server_sync'])
        self.assertFalse(self.drive.writes)
        # A repeat recovers the sheet receipt by readback, with no second write.
        repeated=p.resume_publication(path,sheet_capture=self.capture,drive=None,
            apply=True,server_sync={'enabled':True},sheet_only=True)
        self.assertEqual(repeated['sheet']['state'],'VERIFIED')
        self.assertEqual(len(self.sheet.updates),1)

    def test_http_error_reports_status_without_sensitive_content(self):
        exc=RuntimeError('Bearer PRIVATE_SECRET in https://private.invalid')
        exc.resp=SimpleNamespace(status=429)
        self.assertEqual(p._error(exc),{'error_class':'RuntimeError','http_status':429,'reason':'HTTP_429'})
        self.assertNotIn('PRIVATE_SECRET',json.dumps(p._error(exc)))

    def test_html_response_loss_after_success_reconciles_without_retry(self):
        self.drive.fail='timeout';result=self.run_publish()
        self.assertEqual(result['html']['state'],'VERIFIED');self.assertEqual(len(self.drive.writes),1)
        self.assertEqual(result['html']['write_error_class'],'TimeoutError')
        self.assertNotIn('PRIVATE_SECRET',json.dumps(result))
    def test_crash_after_html_success_recovers_without_reupload(self):
        path=self.freeze();self.drive.fail='crash'
        with self.assertRaises(Crash):self.run_publish(path)
        self.assertEqual(p._load(path/'publication.json')['html']['state'],'ATTEMPTED')
        self.drive.fail=None;result=self.run_publish(path)
        self.assertEqual(result['result'],'PUBLICATION_VERIFIED')
        self.assertEqual(len(self.drive.writes),1);self.assertEqual(len(self.sheet.updates),1)
    def test_unavailable_readback_retains_uncertainty_then_recovers(self):
        path=self.freeze();self.drive.fail='readback';first=self.run_publish(path)
        self.assertEqual(first['html']['state'],'UNCERTAIN')
        self.drive.fail_read=False;self.drive.fail=None
        self.assertEqual(self.run_publish(path)['result'],'PUBLICATION_VERIFIED')
        self.assertEqual(len(self.drive.writes),1)
    def test_independent_html_can_publish_while_sheet_is_blocked(self):
        self.sheet.matrices['Proposed Moves'][0].append('Human notes')
        self.sheet.properties['Proposed Moves']['gridProperties']['columnCount']=18
        result=self.run_publish();self.assertEqual(result['result'],'PUBLICATION_INCOMPLETE')
        self.assertEqual(result['html']['state'],'VERIFIED');self.assertFalse(self.sheet.updates)
        self.assertNotEqual(result['sheet']['state'],'VERIFIED')
    def test_manual_html_edit_between_freeze_and_apply_is_not_overwritten(self):
        path=self.freeze();self.drive.content=b'Human changed HTML'
        result=self.run_publish(path)
        self.assertEqual(result['html']['state'],'CONFLICT');self.assertFalse(self.drive.writes)
    def test_name_or_parent_drift_blocks_without_replacement(self):
        path=self.freeze();self.drive.meta['name']='Human renamed.html'
        result=self.run_publish(path)
        self.assertNotEqual(result['html']['state'],'VERIFIED');self.assertFalse(self.drive.writes)
    def test_mutated_local_artifact_blocks_both_stages(self):
        path=self.freeze();(path/'viewer.html').write_text('changed')
        with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'):self.run_publish(path)
        self.assertFalse(self.sheet.updates);self.assertFalse(self.drive.writes)
    def test_sheet_success_response_loss_preserves_formulas(self):
        self.sheet.timeout_after_update=True
        result=self.run_publish();self.assertEqual(result['result'],'PUBLICATION_VERIFIED')
        self.assertEqual(len(self.sheet.updates),1)
    def test_crash_after_sheet_remote_success_reconciles_without_rewriting(self):
        path=self.freeze();original=self.sheet.batchUpdate
        def crashing(**kw):
            request=original(**kw)
            def run():request.execute(num_retries=0);raise Crash()
            return Request(run)
        self.sheet.batchUpdate=crashing
        with self.assertRaises(Crash):self.run_publish(path)
        self.sheet.batchUpdate=original
        self.assertEqual(self.run_publish(path)['result'],'PUBLICATION_VERIFIED')
        self.assertEqual(len(self.sheet.updates),1)
    def test_fresh_proposal_has_separate_delivery_even_same_journal(self):
        first=self.freeze();self.workbook['exported_at_utc']='2026-02-02T00:00:00Z'
        second=self.freeze();self.assertNotEqual(first,second)
        self.assertEqual(p._load(first/'publication.json')['snapshot_id'],p._load(second/'publication.json')['snapshot_id'])
    def test_registry_multiple_plans_supported_but_unknown_hash_rejected(self):
        spec=self.registry['sections'][0]['publication_spec'];spec['plan_sha256s']=[spec.pop('plan_sha256')]
        self.freeze();spec['plan_sha256s'].append('f'*64)
        with self.assertRaisesRegex(ValueError,'plan history'):self.freeze()
    def test_duplicate_registry_section_rejected(self):
        self.registry['sections'].append(copy.deepcopy(self.registry['sections'][0]))
        with self.assertRaisesRegex(ValueError,'DUPLICATE_REGISTRY'):self.freeze()

    def test_partial_continuation_uses_both_plans_in_journal_order(self):
        ph='b'*64;sid='untracked'
        before={'id':sid,'name':'Old.bin','parents':['old-parent'],'mime_type':'application/octet-stream'}
        target={**before,'name':'New.bin','parents':['destination']}
        op={'operation_id':'continuation','file_id':sid,'action':'MOVE_AND_RENAME','before':before,
            'target':{'name':'New.bin','parents':['destination']},'depends_on':[]}
        self.j.append('DRAFTED',payload={'proposal_hash':ph,'plan_ref':self.j.evidence({'operations':[op]})})
        self.j.append('DESIRED_APPLIED','desired-next',1,{'run_id':'next','plan_sha256':ph,'source_id':sid,
            'direction':'FORWARD','operation_order':1,'dependencies':[],'original_operation_ids':['continuation'],
            'before_ref':self.j.evidence(before),'target_ref':self.j.evidence(target),'observed_ref':self.j.evidence(target),
            'args':{'name':'New.bin','addParents':'destination','removeParents':'old-parent'},'audit_permissions':False})
        spec=self.registry['sections'][0]['publication_spec'];spec['source_ids'].append(sid)
        spec['plan_sha256s']=[ph,spec.pop('plan_sha256')]
        bundle=p.build_publication(self.j,allowed_source_ids=['source-one','untracked'],
            identity_basis=self.config['identity_basis'],sections=p.section_specs(self.registry))
        section=bundle['sections']['sections'][0]
        self.assertEqual(section['status'],'COMPLETED');self.assertEqual(section['completed_file_count'],2)

    def test_nested_reused_folder_overlay_is_not_reclassified_created(self):
        ph='b'*64
        folder={'id':'nested','name':'Nested','parents':['old-parent'],'mime_type':'application/vnd.google-apps.folder'}
        file={'id':'source-one','name':'New.txt','parents':['destination'],'mime_type':'text/plain'}
        targets=[{**folder,'parents':['destination']},{**file,'parents':['nested']}]
        ops=[{'operation_id':'move-'+b['id'],'file_id':b['id'],'action':'MOVE','before':b,
              'target':{'parents':t['parents']},'depends_on':[]} for b,t in zip((folder,file),targets)]
        self.j.append('DRAFTED',payload={'proposal_hash':ph,'plan_ref':self.j.evidence({'operations':ops})})
        for op,t in zip(ops,targets):
            self.j.append('DESIRED_APPLIED','desired-'+op['file_id'],1,{'run_id':'nested-run','plan_sha256':ph,
                'source_id':op['file_id'],'direction':'FORWARD','operation_order':1,'dependencies':[],
                'original_operation_ids':[op['operation_id']],'before_ref':self.j.evidence(op['before']),
                'target_ref':self.j.evidence(t),'observed_ref':self.j.evidence(t),
                'args':{'addParents':t['parents'][0],'removeParents':op['before']['parents'][0]},'audit_permissions':False})
        spec=self.registry['sections'][0]['publication_spec'];spec['plan_sha256s']=[spec.pop('plan_sha256'),ph]
        spec['destination_ids']=['destination','nested'];spec['destination_paths']={'destination':'Root/Section','nested':'Root/Section/Nested'}
        self.config['identity_basis']['source_ids'].append('nested')
        bundle=p.build_publication(self.j,allowed_source_ids=self.config['identity_basis']['source_ids'],
            identity_basis=self.config['identity_basis'],sections=p.section_specs(self.registry))
        self.assertEqual(bundle['sections']['sections'][0]['status'],'COMPLETED')
        nested=next(d for d in bundle['sections']['sections'][0]['verified_destinations'] if d['id']=='nested')
        self.assertEqual(nested['mode'],'REUSED_MOVED')
        self.assertEqual(bundle['sections']['sections'][0]['created_folder_count'],0)
        data=v.unpack(self.drive.content.decode())
        child=copy.deepcopy(data['nodes'][1]);child.update(i=2,p=1,n='Nested',id='nested')
        data['nodes'][2].update(i=3,p=2);data['nodes'][3]['i']=4
        data['nodes'].insert(2,child)
        v.integrate(data,bundle)
        self.assertEqual(data['nodes'][2]['destination_overlay']['mode'],'REUSED_MOVED')
        self.assertEqual(data['nodes'][2]['migration_summary']['status'],'COMPLETE')

    def test_destination_observation_cannot_hide_pending_folder_move(self):
        spec=self.registry['sections'][0]['publication_spec']
        folder={'id':'destination','name':'Section','parents':['old-parent'],'mime_type':'application/vnd.google-apps.folder'}
        op={'operation_id':'move-folder','file_id':'destination','action':'MOVE','before':folder,
            'target':{'parents':['new-parent']},'depends_on':[]}
        ph='b'*64
        self.j.append('DRAFTED',payload={'proposal_hash':ph,'plan_ref':self.j.evidence({'operations':[op]})})
        spec['plan_sha256s']=[spec.pop('plan_sha256'),ph]
        bundle=p.build_publication(self.j,allowed_source_ids=self.config['identity_basis']['source_ids'],
            identity_basis=self.config['identity_basis'],sections=p.section_specs(self.registry))
        self.assertNotEqual(bundle['sections']['sections'][0]['status'],'COMPLETED')


class AdapterTests(unittest.TestCase):
    def test_content_only_exact_id_and_explicit_apply(self):
        calls=[]
        class Files:
            def update(self,**kwargs):calls.append(kwargs);return Request(lambda:{'id':'viewer'})
        transport=SimpleNamespace(_guard=lambda x:None,_ready=lambda **kw:None,service=SimpleNamespace(files=lambda:Files()))
        adapter=p.GooglePublicationDrive(transport,'viewer')
        with self.assertRaisesRegex(ValueError,'APPLY_REQUIRED'):adapter.replace_content('viewer',b'html')
        adapter.apply=True
        with self.assertRaisesRegex(ValueError,'OUTSIDE'):adapter.replace_content('other',b'html')
        adapter.replace_content('viewer',b'html')
        self.assertEqual(set(calls[0]),{'fileId','media_body','fields','supportsAllDrives'})
        self.assertEqual(calls[0]['fileId'],'viewer')


class CommandPublicationContractTests(unittest.TestCase):
    def test_cli_registry_publishes_mixed_intact_and_existing_destination(self):
        from test_destination_command import DestinationCommandTests, raw, SECTION
        from uqmc_recovery.publication_projection import ReadOnlyJournal
        for case, mode in [('mixed','CREATED'),('intact','REUSED_MOVED'),('existing','REUSED_EXISTING')]:
            with self.subTest(case=case):
                fixture=DestinationCommandTests();fixture.setUp()
                try:
                    if case=='mixed':fixture.mixed()
                    if case=='existing':fixture.server.drive.files['existing']=raw('existing','Skills',['club'],True)
                    identities=sorted(fixture.server.drive.files)
                    bundles=[]
                    def publish(registry):
                        bundle=p.build_publication(ReadOnlyJournal(fixture.config['store']),
                            allowed_source_ids=identities,
                            identity_basis={'identity_basis_version':1,'source_ids':identities},
                            sections=p.section_specs(registry))
                        bundles.append(bundle)
                        return {'state':'VERIFIED'}
                    result=fixture.run_command(apply=True,publisher=publish)
                    self.assertEqual(result['publication'],{'state':'VERIFIED'},result)
                    self.assertEqual(len(bundles),1)
                    section=bundles[0]['sections']['sections'][0]
                    self.assertEqual(section['status'],'COMPLETED')
                    self.assertEqual(section['mode'],mode)
                    self.assertEqual(section['completed_file_count'],1)
                    registry=json.loads(Path(fixture.config['registry']).read_text())
                    entry=registry['sections'][0];spec=entry['publication_spec']
                    self.assertEqual(entry['destination_paths'][SECTION],spec['destination_id'])
                    self.assertEqual(spec['destination_paths'][spec['destination_id']],SECTION)
                    if case=='existing':
                        dest=next(d for d in section['verified_destinations'] if d['id']=='existing')
                        self.assertEqual(dest['mode'],'REUSED_EXISTING')
                        self.assertEqual(fixture.server.requests[0]['fileId'],'leaf')
                        self.assertEqual(section['created_folder_count'],0)
                finally:fixture.doCleanups()


if __name__=='__main__':unittest.main()
