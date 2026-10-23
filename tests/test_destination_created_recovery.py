"""Restart a failed preparation without treating its destinations as new sources."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_destination_command as fixture
from test_importer import append_row
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import SafetyError


class CreatedDestinationRecoveryTests(unittest.TestCase):
    def fixture(self):
        f=fixture.DestinationCommandTests();f.setUp();self.addCleanup(f.temp.cleanup)
        f.server.drive.files['incoming']=fixture.raw('incoming','Incoming.pdf',['old-parent'])
        append_row(f.export,'Proposed Renames',{'Rename ID':'RENAME-INCOMING','Source ID':'incoming',
            'Current name + type suffix':'Incoming.pdf','Proposed name + type suffix':'Target.pdf',
            'Proposed destination folder':fixture.SECTION+'/Year'})
        return f

    def interrupt_after_folders(self,f):
        failed=SimpleNamespace(plan=None,manifest={'section_reason_code':None,
            'rows':[{'state':'BLOCKED','reference':'fixture','reason_code':'OFFLINE_INJECTED_IMPORT_FAILURE'}]})
        with patch('uqmc_recovery.section_importer.import_section',return_value=failed):
            with self.assertRaisesRegex(SafetyError,'OFFLINE_INJECTED_IMPORT_FAILURE'):
                f.run_command(apply=True)
        self.assertEqual(len(f.server.created),1)
        self.assertEqual(f.server.requests,[])
        return f.server.created[0]

    def test_failed_preparation_restart_reuses_verified_child_and_imports(self):
        f=self.fixture();child=self.interrupt_after_folders(f)
        result=f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':3},result)
        self.assertEqual(f.server.created,[child])
        self.assertEqual(f.server.drive.files[child]['parents'],['source-root'])
        self.assertEqual(f.server.drive.files['incoming']['parents'],[child])
        self.assertNotIn(child,[x['fileId'] for x in f.server.requests])
        self.assertEqual(f.server.drive.files['source-root']['parents'],['club'])
        order=[x['fileId'] for x in f.server.requests]
        self.assertLess(order.index('source-root'),order.index('incoming'))

    def interrupted_creation_before_parent_path_record(self):
        from uqmc_recovery.folder_creation import prepare_required_folders
        from uqmc_recovery.direct_transport import TransportError
        f=self.fixture();f.mixed()
        f.server.drive.files['existing']=fixture.raw('existing','Skills',['club'],True)
        def interrupted(*args,**kwargs):
            prepare_required_folders(*args,**kwargs)
            raise TransportError('DRIVE_REQUEST_TIMEOUT',request='files.list')
        with patch('uqmc_recovery.folder_creation.prepare_required_folders',side_effect=interrupted):
            with self.assertRaisesRegex(TransportError,'DRIVE_REQUEST_TIMEOUT'):
                f.run_command(apply=True)
        self.assertEqual(len(f.server.created),1)
        self.assertEqual(f.server.requests,[])
        self.assertFalse(any(e['kind']=='DESTINATION_VERIFIED'
                             for e in Journal(f.config['store']).records()))
        return f

    def test_verified_child_recovers_existing_parent_before_path_record(self):
        f=self.interrupted_creation_before_parent_path_record()
        child=f.server.created[0];before=copy.deepcopy(f.server.drive.files)
        preview=f.run_command()
        self.assertEqual(preview['blockers'],[])
        self.assertEqual(preview['missing_folders'],[])
        self.assertEqual(f.server.drive.files,before)
        result=f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(f.server.created,[child])
        self.assertEqual(f.server.drive.files[child]['parents'],['existing'])
        self.assertEqual(f.server.drive.files['incoming']['parents'],[child])

    def test_unrecorded_parent_recovery_rejects_changed_name_parent_and_collision(self):
        for change in ('name','parent','collision'):
            with self.subTest(change=change):
                f=self.interrupted_creation_before_parent_path_record()
                if change=='name':f.server.drive.files['existing']['name']='Changed'
                elif change=='parent':f.server.drive.files['existing']['parents']=['old-parent']
                else:f.server.drive.files['duplicate']=fixture.raw('duplicate','Skills',['club'],True)
                with self.assertRaisesRegex(SafetyError,'RECOVERED_DESTINATION_PARENT_PATH_CONFLICT'):
                    f.run_command()
                self.assertEqual(f.server.requests,[])

    def test_first_preparation_includes_own_created_child_in_scope_without_source_row(self):
        f=self.fixture();result=f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':3},result)
        self.assertEqual(len(f.server.created),1)
        self.assertEqual(len(f.server.requests),3)

    def test_unplanned_content_beneath_journaled_destination_still_blocks(self):
        f=self.fixture();child=self.interrupt_after_folders(f)
        f.server.drive.files['unplanned']=fixture.raw('unplanned','Unplanned.pdf',[child])
        with self.assertRaisesRegex(SafetyError,'WHOLE_SUBTREE_CHILD_MISSING_PROPOSAL_COVERAGE:source-root:unplanned'):
            f.run_command(apply=True)
        self.assertEqual(f.server.requests,[])
        self.assertEqual(f.server.created,[child])

    def test_recorded_id_with_changed_name_or_parent_or_key_stays_blocked(self):
        for field,value in [('name','Unexpected'),('parents',['old-parent']),('appProperties',{'uqmcFolderKey':'different'})]:
            with self.subTest(field=field):
                f=self.fixture();child=self.interrupt_after_folders(f)
                f.server.drive.files[child][field]=value
                with self.assertRaisesRegex(SafetyError,'RECOVERED_DESTINATION_METADATA_CONFLICT'):
                    f.run_command(apply=True)
                self.assertEqual(f.server.requests,[])

    def test_changed_approved_path_does_not_repurpose_old_created_identity(self):
        f=self.fixture();child=self.interrupt_after_folders(f)
        rows=f.export['sheets']['Proposed Renames'];heads=rows[0]
        for row in rows[1:]:
            if row[heads.index('Source ID')]=='incoming':row[heads.index('Proposed destination folder')]=fixture.SECTION+'/Different'
        with self.assertRaisesRegex(SafetyError,'RECOVERED_DESTINATION_PATH_NO_LONGER_APPROVED'):
            f.run_command(apply=True)
        self.assertEqual(f.server.created,[child]);self.assertEqual(f.server.requests,[])

    def test_parent_scope_failed_preparation_can_preview_then_import_smaller_subtree(self):
        from uqmc_recovery.destination import run_destination, section_key
        f=self.fixture()
        f.config['root_id']='project-root'
        f.server.drive.files['project-root']=fixture.raw('project-root','Project',[],True)
        f.server.drive.files['club']['parents']=['project-root']
        failed=SimpleNamespace(plan=None,manifest={'rows':[{'state':'BLOCKED',
            'reference':'fixture','reason_code':'OFFLINE_INJECTED_IMPORT_FAILURE'}]})
        with patch('uqmc_recovery.section_importer.import_section',return_value=failed):
            with self.assertRaisesRegex(SafetyError,'OFFLINE_INJECTED_IMPORT_FAILURE'):
                run_destination(f.config,'Club',apply=True,capture_client=f.capture,
                    factory=f.server.factory,publisher=f.publisher,report=f.progress.append)
        self.assertEqual(len(f.server.created),1)
        child=f.server.created[0]
        before=copy.deepcopy(f.server.drive.files)
        preview=f.run_command()
        self.assertTrue(preview['preview_only'])
        self.assertEqual(preview['missing_folders'],[])
        self.assertEqual(f.server.drive.files,before)
        self.assertEqual(f.server.requests,[])
        result=f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':3},result)
        self.assertEqual(f.server.created,[child])
        self.assertTrue(result['run_id'].startswith(section_key(fixture.SECTION)+'-'))
        self.assertFalse(result['run_id'].startswith(section_key('Club')+'-'))

    def test_creation_without_verification_is_not_coverage_authority(self):
        f=self.fixture();original=Journal.append
        def crash(journal,kind,*args,**kwargs):
            if kind=='REQUIRED_FOLDER_VERIFIED':raise KeyboardInterrupt()
            return original(journal,kind,*args,**kwargs)
        with patch.object(Journal,'append',crash),self.assertRaises(KeyboardInterrupt):
            f.run_command(apply=True)
        self.assertEqual(len(f.server.created),1)
        with self.assertRaisesRegex(SafetyError,'WHOLE_SUBTREE_CHILD_MISSING_PROPOSAL_COVERAGE'):
            f.run_command(apply=True)
        self.assertEqual(f.server.requests,[])
        self.assertEqual(len(f.server.created),1)

    def test_different_section_creation_cannot_grant_source_coverage(self):
        f=self.fixture();child=self.interrupt_after_folders(f)
        # A different section has no matching verified creation lineage.
        from uqmc_recovery.destination_recovery import load_created_folder_proofs
        self.assertEqual(load_created_folder_proofs(Journal(f.config['store']),'different-section'),{})


if __name__=='__main__':unittest.main()
