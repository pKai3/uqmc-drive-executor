"""Small offline controller/collection tests; no Google or credential access."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_importer import export_fixture, metadata
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, Operation, Plan, SafetyError, digest
from uqmc_recovery.section_execution import collect_metadata, normalize, prepare, main, config_binding


def raw(sid, name, parents, *, folder=False):
    return {'id':sid,'name':name,'parents':parents,'mimeType':FOLDER if folder else 'application/pdf',
        'version':'1','modifiedTime':'2026-10-21T00:00:00Z','trashed':False,
        'owners':[{'permissionId':'fixture-owner'}],
        'capabilities':{'canRename':True,'canAddChildren':True}}


class FakeClient:
    def __init__(self, files, allowed, calls):
        self.files,self.allowed,self.calls=files,set(allowed),calls
    def get_file(self, sid):
        if sid not in self.allowed: raise AssertionError('Out-of-scope metadata read')
        self.calls.append(('get',sid));return copy.deepcopy(self.files[sid])
    def list_children(self, sid):
        if sid not in self.allowed:raise AssertionError('Out-of-scope listing')
        self.calls.append(('list',sid))
        return [{key:copy.deepcopy(v[key]) for key in ('id','name','mimeType','parents')}
            for v in self.files.values() if sid in v['parents']]
    def list_permissions(self,*args):raise AssertionError('No permissions listing authorized')
    def get_account(self):return {'emailAddress':'fixture@example.test'}


class SectionExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.calls=[]
        self.files={'anchor':raw('anchor','Club',[],folder=True),
            'old':raw('old','Old',[],folder=True),
            'outer':raw('outer','Outer',['old'],folder=True),
            'inner':raw('inner','Inner',['outer'],folder=True),
            'leaf':raw('leaf','Leaf.pdf',['inner']),
            'outside':raw('outside','Untouched.pdf',['old'])}
    def factory(self,allowed):return FakeClient(self.files,allowed,self.calls)

    def test_nested_metadata_has_full_members_without_reading_unrelated_sibling(self):
        result=collect_metadata({'outer','inner','leaf'},self.factory,workers=2)
        self.assertEqual({m['id'] for m in result['metadata']['outer']['subtree']['members']},{'inner','leaf'})
        self.assertEqual(result['metadata']['outer']['subtree']['membership_scope'],'RECURSIVE')
        self.assertEqual(result['metadata']['old']['subtree']['membership_scope'],'DIRECT_CHILDREN')
        self.assertNotIn(('get','outside'),self.calls)
        self.assertEqual(self.calls.count(('get','outer')),1)
        self.assertEqual(self.calls.count(('list','outer')),1)
        self.assertEqual(result['source_ids'],['inner','leaf','outer'])

    def test_preview_cache_reuses_completed_exact_reads_without_callbacks(self):
        cache={};callbacks=[]
        first=collect_metadata({'outer','inner','leaf'},self.factory,workers=2,
            read_cache=cache,on_read=lambda method,sid,value:callbacks.append((method,sid,value)))
        self.assertEqual(len(callbacks),len(self.calls))
        self.assertIn('get_file:leaf',cache)
        self.assertIn('list_children:inner',cache)
        self.calls.clear();callbacks.clear()
        second=collect_metadata({'outer','inner','leaf'},self.factory,workers=2,
            read_cache=cache,on_read=lambda *args:callbacks.append(args))
        self.assertEqual(first,second)
        self.assertEqual(self.calls,[])
        self.assertEqual(callbacks,[])
        second['raw']['leaf']['name']='local mutation'
        self.assertEqual(cache['get_file:leaf']['name'],'Leaf.pdf')

    def test_preview_timeout_retries_only_missing_listing(self):
        cache={};persisted={}
        def capture(method,sid,value):
            key=method+':'+sid
            self.assertEqual(cache[key],value)
            persisted[key]=copy.deepcopy(value)
        def fail_factory(allowed):
            client=self.factory(allowed)
            original=client.list_children
            def listing(sid):
                if sid=='outer':
                    self.calls.append(('list',sid));raise TimeoutError('fixture timeout')
                return original(sid)
            client.list_children=listing
            return client
        with self.assertRaises(TimeoutError):
            collect_metadata({'outer','inner','leaf'},fail_factory,workers=1,read_cache=cache,on_read=capture)
        self.assertNotIn('list_children:outer',persisted)
        self.assertIn('get_file:leaf',persisted)
        self.assertIn('list_children:inner',persisted)
        self.calls.clear()
        result=collect_metadata({'outer','inner','leaf'},self.factory,workers=1,read_cache=persisted)
        self.assertEqual(self.calls,[('list','outer')])
        self.assertEqual(result['source_ids'],['inner','leaf','outer'])

    def test_cache_rejects_wrong_identity_or_parent_and_ignores_unrequested_keys(self):
        with self.assertRaisesRegex(SafetyError,'INVALID_SECTION_READ_CACHE:get_file:leaf'):
            collect_metadata({'leaf'},self.factory,workers=1,read_cache={'get_file:leaf':self.files['outside']})
        self.assertEqual(self.calls,[])
        cache={'get_file:outside':{'malformed':'unused'},'list_children:inner':[
            {'id':'wrong-child','parents':['outside']}]}
        with self.assertRaisesRegex(SafetyError,'INVALID_SECTION_READ_CACHE:list_children:inner'):
            collect_metadata({'leaf'},self.factory,workers=1,read_cache=cache)
        self.assertNotIn(('get','outside'),self.calls)

    def test_no_cache_keeps_fresh_reads_and_callback_receives_copies(self):
        callbacks=[]
        def capture(method,sid,value):
            callbacks.append((method,sid));value.clear()
        first=collect_metadata({'leaf'},self.factory,workers=1,on_read=capture)
        self.assertEqual(first['raw']['leaf']['name'],'Leaf.pdf')
        calls=list(self.calls);self.calls.clear()
        collect_metadata({'leaf'},self.factory,workers=1)
        self.assertEqual(self.calls,calls)
        self.assertEqual(len(callbacks),len(calls))

    def test_unknown_descendant_stops_before_individual_metadata_read(self):
        with self.assertRaisesRegex(SafetyError,'UNAPPROVED_FOLDER_DESCENDANT'):
            collect_metadata({'outer'},self.factory,workers=1)
        self.assertNotIn(('get','inner'),self.calls)
        self.assertNotIn(('get','leaf'),self.calls)
        self.assertNotIn(('list','inner'),self.calls)

    def test_destination_scoped_collection_preserves_mixed_source_folder(self):
        self.files['unrelated-eoi']=raw('unrelated-eoi','Unrelated EOI.pdf',['inner'])
        original=copy.deepcopy(self.files)
        result=collect_metadata({'inner','leaf'},self.factory,workers=2,recursive_sources=False)
        self.assertEqual(result['metadata']['leaf']['parents'],['inner'])
        self.assertEqual(result['metadata']['leaf']['name'],'Leaf.pdf')
        self.assertEqual(result['metadata']['inner']['subtree']['membership_scope'],'DIRECT_CHILDREN')
        self.assertEqual({m['id'] for m in result['metadata']['inner']['subtree']['members']},
            {'leaf','unrelated-eoi'})
        self.assertNotIn(('get','unrelated-eoi'),self.calls)
        self.assertNotIn(('list','unrelated-eoi'),self.calls)
        self.assertNotIn('unrelated-eoi',result['metadata'])
        self.assertEqual(self.files,original)
        self.calls.clear()
        with self.assertRaisesRegex(SafetyError,'UNAPPROVED_FOLDER_DESCENDANT:inner:unrelated-eoi'):
            collect_metadata({'inner','leaf'},self.factory,workers=2)
        self.assertNotIn(('get','unrelated-eoi'),self.calls)

    def test_excluded_descendant_never_individually_read(self):
        with self.assertRaisesRegex(SafetyError,'EXCLUDED_FOLDER_DESCENDANT'):
            collect_metadata({'inner'},self.factory,excluded_ids={'leaf'},workers=1)
        self.assertNotIn(('get','leaf'),self.calls)

    def test_excluded_selected_scope_rejected_before_any_read(self):
        with self.assertRaisesRegex(SafetyError,'INVALID_OR_EXCLUDED_SECTION_READ_SCOPE'):
            collect_metadata({'leaf'},self.factory,excluded_ids={'leaf'})
        self.assertEqual(self.calls,[])

    def test_normalization_preserves_known_before_image_without_aliasing(self):
        before=raw('leaf','Before.pdf',['inner']);before.update(size='123',md5Checksum='fixture-hash')
        observed=normalize(before)
        before['name']='After.pdf';before['parents'].append('changed')
        self.assertEqual(observed['name'],'Before.pdf')
        self.assertEqual(observed['parents'],['inner'])
        self.assertEqual(observed['md5_checksum'],'fixture-hash')
        self.assertEqual(observed['size'],'123')
        self.assertEqual(observed['modified_time'],'2026-10-21T00:00:00Z')
        self.assertTrue(observed['parents_complete'])

    def test_preview_uses_one_capture_and_does_not_write_source_or_journal_events(self):
        capture=export_fixture();capture['source_revision']={'version':'fixture'}
        self.files['source']=raw('source','Minutes',['old'])
        journal=Journal(self.root/'state')
        config={'run_directory':str(self.root/'run'),'token':'unused-private-fixture',
            'workbook_id':capture['workbook_id'],'section_path':'Club/Archive','section_id':'archive',
            'anchor_paths':{'Club':'anchor'},'workers':2}
        def factory(allowed,mutations,excluded,apply):
            self.assertFalse(apply);self.assertFalse(mutations)
            return self.factory(allowed)
        with patch('uqmc_recovery.sheets_capture.GoogleSheetsCapture.from_authorized_user_file') as capture_factory:
            capture_factory.return_value.capture.return_value=capture
            result=prepare(journal,config,factory,apply=False)
        self.assertEqual(capture_factory.return_value.capture.call_count,1)
        self.assertTrue(result['preview_only'])
        self.assertEqual(result['source_ids'],['source'])
        self.assertEqual(journal.records(),[])
        self.assertNotIn(('get','outside'),self.calls)

    def test_prior_allocation_never_adopts_different_same_name_folder(self):
        capture=export_fixture();capture['source_revision']={'version':'fixture'}
        self.files['source']=raw('source','Minutes',['old'])
        self.files['unrelated-folder']=raw('unrelated-folder','Archive',['anchor'],folder=True)
        journal=Journal(self.root/'state')
        key='folder:'+digest({'section_path':'Club/Archive','logical_path':'Club/Archive'})
        journal.append('REQUIRED_FOLDER_ALLOCATED',payload={'section_id':'archive','key':key,
            'file_id':'reserved-expected'})
        config={'run_directory':str(self.root/'run'),'token':'unused-private-fixture',
            'workbook_id':capture['workbook_id'],'section_path':'Club/Archive','section_id':'archive',
            'anchor_paths':{'Club':'anchor'},'workers':2}
        def factory(allowed,mutations,excluded,apply):
            self.assertFalse(apply);return self.factory(allowed)
        with patch('uqmc_recovery.sheets_capture.GoogleSheetsCapture.from_authorized_user_file') as capture_factory:
            capture_factory.return_value.capture.return_value=capture
            result=prepare(journal,config,factory,apply=False)
        self.assertNotIn('Club/Archive',result['destination_paths'])
        self.assertEqual([x['logical_key'] for x in result['missing_folders']],[key])
        self.assertNotIn(('get','unrelated-folder'),self.calls)
        self.assertEqual(len(journal.records()),1)

    def run_resume(self, changed_section=False):
        journal=Journal(self.root/'state')
        run=self.root/'run';run.mkdir()
        (self.root/'backups').mkdir()
        plan=Plan('fixture-run','fixture-run','fixture',(Operation('op','source','RENAME',
            metadata('source','Old.pdf','application/pdf'),{'name':'New.pdf'}),))
        plan_path=run/'plan.json';plan_path.write_text(json.dumps(plan.body()))
        prepared={'plan_path':str(plan_path),'plan_sha256':plan.proposal_hash,'run_id':plan.run_id,
            'source_ids':['source'],'allowed_ids':['source','old'],'source_revision':{'version':'fixture'},
            'destination_paths':{'Club/Archive':'destination'},'new_folder_count':0,
            'section_id':'archive','section_path':'Club/Archive','workbook_id':'fixture-workbook',
            'expected_account':'fixture@example.test'}
        config={'run_directory':str(run),'store':str(journal.root),'token':'unused-private-fixture',
            'backup_directory':str(self.root/'backups'),'anchor_paths':{'Club':'anchor'},
            'expected_account':'fixture@example.test','section_id':'archive','section_path':'Club/Archive',
            'workbook_id':'fixture-workbook','workers':2}
        prepared['config_binding_sha256']=config_binding(config)
        (run/'prepared-batch.json').write_text(json.dumps(prepared))
        if changed_section: config['section_id']='unrelated'
        config_path=self.root/'config.json';config_path.write_text(json.dumps(config))
        class FakeRunner:
            def __init__(self,journal,*args,**kwargs):self.journal=journal
            def run(self,**kwargs):
                with self.journal.shared_locked():
                    self.journal.append('FIXTURE_FINISHED',payload={})
                    return {'items':[{'source_id':'source','state':'TARGET','effects':{}}],
                        'counts':{'TARGET':1}}
        stdout=io.StringIO()
        with patch('uqmc_recovery.direct_transport.GoogleDriveTransport.from_authorized_user_file',
                return_value=self.factory({'anchor'})),\
             patch('uqmc_recovery.batch_source.require_batch_source'),\
             patch('uqmc_recovery.desired.DesiredRunner',FakeRunner),\
             patch('uqmc_recovery.desired.export_status',return_value={}),\
             patch('uqmc_recovery.section_execution.signal.signal'),contextlib.redirect_stdout(stdout):
            code=main(['--config',str(config_path),'--apply','--resume'])
        return code,stdout.getvalue()

    def test_resume_controller_allows_runner_to_acquire_shared_writer_lock(self):
        code,output=self.run_resume()
        self.assertEqual(code,0,output)
        self.assertIn('"backup_verified": true',output)

    def test_execution_method_is_part_of_resume_configuration_binding(self):
        config={'section_id':'archive','section_path':'Club/Archive','workbook_id':'fixture',
            'expected_account':'fixture@example.test','anchor_paths':{'Club':'anchor'}}
        original=config_binding(config)
        self.assertEqual(original,config_binding(config | {'method_profile':'approved_nested_section'}))
        self.assertNotEqual(original,config_binding(config | {'method_profile':'destination_scoped_files'}))

    def test_resume_rejects_different_section_configuration(self):
        code,output=self.run_resume(changed_section=True)
        self.assertEqual(code,2)
        self.assertIn('PREPARED_BATCH_CONFIG_MISMATCH',output)


if __name__=='__main__':unittest.main()
