"""Ancestor destination evidence is reusable, but never a new mutation grant."""
import copy
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from uqmc_recovery.destination_recovery import verified_destination_anchors
from uqmc_recovery.destination import run_destination, section_key
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, SafetyError
import test_destination_command as fixture
from test_importer import append_row


class AnchorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.j=Journal(self.tmp.name)
        self.path='Club/Digital/Social';self.raw=dict(id='parent',name='Social',parents=['digital'],
            mime_type=FOLDER,trashed=False,appProperties={'uqmcFolderKey':'fixture-key'})
    def record(self,section='Club/Digital',sid='parent',path=None):
        raw={**self.raw,'id':sid}
        self.j.append('DESTINATION_VERIFIED',payload={'section_id':section_key(section),'file_id':sid,
            'proposal_path':path or self.path,'exact_name':raw['name'],'parent_ids':raw['parents'],
            'observed_ref':self.j.evidence(raw)})
    def read(self,metadata=None,ids=()):
        return verified_destination_anchors(self.j,self.path,metadata or {'parent':self.raw},ids)
    def test_ancestor_and_exact_scope_reuse_without_journal_changes(self):
        for section in ['Club','Club/Digital',self.path]:
            self.record(section)
        count=len(self.j.records());self.assertEqual(self.read(),{self.path:'parent'})
        self.assertEqual(len(self.j.records()),count)
    def test_sibling_scope_outside_path_and_source_proposals_not_adopted(self):
        self.record('Club/Other');self.record(path='Club/Digital/Other')
        self.assertEqual(self.read(),{})
        self.record();self.assertEqual(self.read(ids={'parent'}),{})
    def test_current_parent_name_identity_type_trash_and_known_marker_must_match(self):
        self.record()
        for key,value in [('id','different'),('name','Changed'),('parents',['different']),
                          ('mime_type','application/pdf'),('trashed',True),('appProperties',{}),
                          ('appProperties',{'uqmcFolderKey':'changed'})]:
            with self.subTest(key=key,value=value):
                self.assertEqual(self.read({'parent':{**self.raw,key:value}}),{})
    def test_different_id_collision_fails(self):
        self.record();self.record(sid='different')
        with self.assertRaisesRegex(SafetyError,'ANCHOR_AMBIGUOUS'):
            self.read({'parent':self.raw,'different':{**self.raw,'id':'different'}})


class BroadPreparationNarrowPreviewTests(unittest.TestCase):
    def fixture(self):
        f=fixture.DestinationCommandTests();f.setUp();self.addCleanup(f.doCleanups);f.mixed()
        f.config['root_id']='project'
        f.server.drive.files['project']=fixture.raw('project','Project',[],True)
        f.server.drive.files['club']['parents']=['project']
        f.server.drive.files['existing']=fixture.raw('existing','Skills',['club'],True)
        f.server.drive.files['incoming']=fixture.raw('incoming','Incoming.pdf',['old-parent'])
        append_row(f.export,'Proposed Renames',{'Rename ID':'RENAME-INCOMING','Source ID':'incoming',
            'Current name + type suffix':'Incoming.pdf','Proposed name + type suffix':'Target.pdf',
            'Proposed destination folder':fixture.SECTION+'/Year'})
        failed=SimpleNamespace(plan=None,manifest={'rows':[{'state':'BLOCKED','reference':'fixture',
            'reason_code':'OFFLINE_INJECTED_IMPORT_FAILURE'}]})
        with patch('uqmc_recovery.section_importer.import_section',return_value=failed):
            with self.assertRaisesRegex(SafetyError,'OFFLINE_INJECTED_IMPORT_FAILURE'):
                run_destination(f.config,'Club',apply=True,capture_client=f.capture,
                    factory=f.server.factory,publisher=f.publisher,report=f.progress.append)
        self.assertEqual(f.server.requests,[])
        return f
    def test_verified_existing_parent_and_created_child_survive_narrow_selection(self):
        f=self.fixture();before=copy.deepcopy(f.server.drive.files);created=list(f.server.created)
        result=f.run_command()
        self.assertTrue(result['preview_only']);self.assertEqual(result['blockers'],[])
        self.assertEqual(result['missing_folders'],[])
        self.assertEqual(f.server.drive.files,before);self.assertEqual(f.server.created,created)
        self.assertEqual(f.server.requests,[])
    def test_real_parent_change_still_blocks_narrow_selection(self):
        f=self.fixture();f.server.drive.files['existing']['parents']=['old-parent']
        with self.assertRaisesRegex(SafetyError,'RECOVERED_DESTINATION_PARENT_PATH_CONFLICT'):
            f.run_command()
        self.assertEqual(f.server.requests,[])
