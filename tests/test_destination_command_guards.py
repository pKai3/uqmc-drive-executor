"""Offline destination-command authority and recovery boundary regressions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_importer import append_row
from uqmc_recovery.destination import (read_and_plan, run_destination, semantic_selection,
    validate_resume_scope, section_key, RevisionGuard)
from uqmc_recovery.importer import HEADERS, WORKBOOK_ID, catalog
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, SafetyError, digest
from uqmc_recovery.section_execution import normalize


class Capture:
    def __init__(self,value):self.value=value;self.calls=0
    def capture(self):self.calls+=1;return copy.deepcopy(self.value)
    def _revision(self):return self.value['source_revision']


class Client:
    def __init__(self,files):self.files=files
    def get_account(self):return {'emailAddress':'fixture@example.com'}
    def get_file(self,sid):return copy.deepcopy(self.files[sid])
    def list_children(self,sid):return [copy.deepcopy(x) for x in self.files.values() if sid in x['parents']]


class DestinationCommandGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.journal=Journal(self.root/'state')
        self.export={'workbook_id':WORKBOOK_ID,'exported_at_utc':'2026-01-01T00:00:00Z',
            'complete':{tab:True for tab in HEADERS},'sheets':{tab:[list(headers)] for tab,headers in HEADERS.items()},
            'source_revision':{'version':'1'}}
        append_row(self.export,'Proposed Renames',{'Rename ID':'R-file','Source ID':'file',
            'Current name + type suffix':'Original.pdf','Proposed name + type suffix':'Approved.pdf',
            'Proposed destination folder':'Club/Skills'})
        self.config={'workbook_id':WORKBOOK_ID,'store':str(self.root/'state'),'registry':str(self.root/'registry.json'),
            'run_directory':str(self.root/'runs'),'backup_directory':str(self.root/'backups'),'anchor_paths':{'Club':'club'},
            'expected_account':'fixture@example.com','accepted_inherited_access':True,'workers':1}
        def raw(sid,name,mime,parents):return {'id':sid,'name':name,'mimeType':mime,'parents':parents,'trashed':False,
            'version':'1','capabilities':{'canRename':True,'canMoveItemWithinDrive':True,'canAddChildren':True},'size':'12'}
        self.files={'file':raw('file','Original.pdf','application/pdf',['old']),
            'old':raw('old','Old',FOLDER,[]),'club':raw('club','Club',FOLDER,[])}
        self.clients=[]
        def factory(allowed,mutations,excluded,enabled,**kwargs):
            self.clients.append({'allowed':set(allowed),'mutations':set(mutations),'enabled':enabled})
            return Client(self.files)
        self.factory=factory

    def source(self):return catalog(self.export)

    def prepared(self):
        return {'run_id':'saved-run','section_path':'Club/Skills','plan_path':str(self.root/'never-read-plan.json'),
            'selected_source_ids':['file'],'semantic_selection':semantic_selection(self.source(),'Club/Skills')}

    def unrelated_no(self,source_id='file'):
        append_row(self.export,'Proposed Moves',{'Proposal ID':'M-other','Action':'Move','Decision (Yes/No)':'No'})
        append_row(self.export,'Move Manifest',{'Proposal ID':'M-other','Manifest item':'I-other',
            'Source ID':source_id,'Exact target path':'Club/Other/Name.pdf'})

    def test_no_on_another_action_for_same_source_blocks_resume(self):
        saved=self.prepared();self.unrelated_no()
        with self.assertRaisesRegex(SafetyError,'RESUME_SOURCE_AUTHORITY_REMOVED:file'):
            validate_resume_scope(self.source(),'Club/Skills',saved,self.journal)

    def test_unrelated_no_does_not_stop_selected_section(self):
        saved=self.prepared();self.unrelated_no('unrelated')
        validate_resume_scope(self.source(),'Club/Skills',saved,self.journal)

    def test_durable_source_exclusion_blocks_resume(self):
        saved=self.prepared();self.journal.append('EXCLUDED',payload={'file_id':'file'})
        with self.assertRaisesRegex(SafetyError,'RESUME_SOURCE_AUTHORITY_REMOVED:file'):
            validate_resume_scope(self.source(),'Club/Skills',saved,self.journal)

    def test_durable_proposal_exclusion_blocks_resume(self):
        saved=self.prepared();self.journal.append('PROPOSAL_EXCLUDED',payload={'proposal_ref':'R-file'})
        with self.assertRaisesRegex(SafetyError,'RESUME_SOURCE_AUTHORITY_REMOVED:file'):
            validate_resume_scope(self.source(),'Club/Skills',saved,self.journal)

    def test_changed_target_stops_resume(self):
        saved=self.prepared()
        self.export['sheets']['Proposed Renames'][1][HEADERS['Proposed Renames'].index('Proposed name + type suffix')]='Changed.pdf'
        with self.assertRaisesRegex(SafetyError,'PENDING_PROPOSAL_CHANGED'):
            validate_resume_scope(self.source(),'Club/Skills',saved,self.journal)

    def test_resume_checks_authority_before_plan_loading_or_mutation_transport(self):
        saved=self.prepared();self.unrelated_no()
        Path(self.config['registry']).write_text(json.dumps({'sections':[],'runs':[saved]}))
        with patch('uqmc_recovery.destination.plan_from_json',side_effect=AssertionError('must not reach plan')),self.assertRaisesRegex(SafetyError,'RESUME_SOURCE_AUTHORITY_REMOVED'):
            run_destination(self.config,'Club/Skills',apply=True,resume=True,capture_client=Capture(self.export),factory=self.factory)
        self.assertFalse(any(c['enabled'] for c in self.clients))

    def test_rollback_without_apply_is_only_saved_run_preview(self):
        saved=self.prepared();Path(self.config['registry']).write_text(json.dumps({'sections':[],'runs':[saved]}))
        with patch('uqmc_recovery.destination.authorize',side_effect=AssertionError('must not authorize')):
            result=run_destination(self.config,'Club/Skills',rollback='saved-run',capture_client=Capture(self.export),factory=self.factory)
        self.assertTrue(result['preview_only']);self.assertEqual(result['saved_run_id'],'saved-run')
        self.assertFalse(any(c['enabled'] for c in self.clients))

    def test_wrong_account_stops_before_sheet_capture(self):
        config={**self.config,'expected_account':'wrong@example.com'};capture=Capture(self.export)
        with self.assertRaisesRegex(SafetyError,'AUTHENTICATED_ACCOUNT_MISMATCH'):
            run_destination(config,'Club/Skills',capture_client=capture,factory=self.factory)
        self.assertEqual(capture.calls,0)

    def test_missing_destination_does_not_accept_source_name_drift(self):
        self.files['file']['name']='Human edit.pdf'
        result=read_and_plan(self.config,'Club/Skills',self.journal,self.export,self.factory,{'sections':[]},lambda e:None)
        self.assertEqual(result['checks'][0]['disposition'],'BLOCKED')
        self.assertEqual(result['checks'][0]['reason'],'UNAPPROVED_CURRENT_STATE:name')
        self.assertFalse(any(c['enabled'] for c in self.clients))


    def test_interrupted_folder_creation_keeps_reserved_id_recovery_path(self):
        logical='Club/Skills';key='folder:'+digest({'section_path':logical,'logical_path':logical})
        self.journal.append('REQUIRED_FOLDER_ALLOCATED',payload={'section_id':section_key(logical),'key':key,'file_id':'allocated'})
        self.files['allocated']={'id':'allocated','name':'Skills','mimeType':FOLDER,'parents':['club'],
            'trashed':False,'capabilities':{'canAddChildren':True}}
        result=read_and_plan(self.config,logical,self.journal,self.export,self.factory,{'sections':[]},lambda e:None)
        self.assertEqual([x['logical_key'] for x in result['layout']['missing_folders']],[key])
        self.assertNotIn(logical,result['layout']['destination_paths'])
        self.assertFalse(any(c['enabled'] for c in self.clients))

    def test_same_name_other_id_cannot_replace_interrupted_allocation(self):
        logical='Club/Skills';key='folder:'+digest({'section_path':logical,'logical_path':logical})
        self.journal.append('REQUIRED_FOLDER_ALLOCATED',payload={'section_id':section_key(logical),'key':key,'file_id':'allocated'})
        self.files['other']={'id':'other','name':'Skills','mimeType':FOLDER,'parents':['club'],
            'trashed':False,'capabilities':{'canAddChildren':True}}
        result=read_and_plan(self.config,logical,self.journal,self.export,self.factory,{'sections':[]},lambda e:None)
        self.assertNotIn('other',result['layout']['destination_paths'].values())
        self.assertEqual([x['logical_key'] for x in result['layout']['missing_folders']],[key])

    def test_revision_change_stops_scheduling_without_mutation_or_refresh(self):
        capture=Capture(self.export);reported=[]
        guard=RevisionGuard(capture,{'version':'previous'},reported.append,interval=0)
        class Runner:stop_requested=False
        guard.runner=Runner()
        guard({'stage':'fixture_progress'})
        self.assertTrue(guard.runner.stop_requested)
        self.assertEqual(guard.reason,'WORKBOOK_CHANGED_REFRESH_REQUIRED')
        self.assertEqual(capture.calls,0)
        self.assertEqual(reported[-1]['stage'],'scheduling_stopped')


    def test_durable_excluded_proposal_is_filtered_before_metadata_read(self):
        self.journal.append('PROPOSAL_EXCLUDED',payload={'proposal_ref':'R-file'})
        append_row(self.export,'Proposed Renames',{'Rename ID':'R-other','Source ID':'other',
            'Current name + type suffix':'Other.pdf','Proposed name + type suffix':'Other approved.pdf',
            'Proposed destination folder':'Club/Skills'})
        self.files['other']={**self.files['file'],'id':'other','name':'Other.pdf'}
        result=read_and_plan(self.config,'Club/Skills',self.journal,self.export,self.factory,{'sections':[]},lambda e:None)
        self.assertEqual([x['source_id'] for x in result['checks']],['other'])
        self.assertFalse(any('file' in c['allowed'] for c in self.clients))
        self.assertIn('R-file',result['source']['excluded_refs'])


if __name__=='__main__':unittest.main()
