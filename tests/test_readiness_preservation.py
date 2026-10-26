"""Preserve unchanged archive siblings and exact journaled destination aliases."""
import copy
import unittest

import test_destination_command as command
from test_importer import append_row
from test_section_prepare import rec, source, state
from uqmc_recovery.destination_planner import plan_destination
from uqmc_recovery.model import FOLDER, SafetyError, digest


class ArchiveSiblingTests(unittest.TestCase):
    def fixture(self):
        f=command.DestinationCommandTests();f.setUp();self.addCleanup(f.temp.cleanup)
        f.server.drive.files['leaf']['name']='Approved.pdf'
        f.server.drive.files['copy']=command.raw('copy','Approved.pdf',['source-root'])
        append_row(f.export,'Proposed Renames',{'Rename ID':'RENAME-COPY','Source ID':'copy',
            'Current name + type suffix':'Approved.pdf','Proposed name + type suffix':'Approved.pdf',
            'Proposed destination folder':command.SECTION})
        return f

    def test_intact_archive_moves_ancestor_without_touching_duplicate_siblings(self):
        f=self.fixture();before=copy.deepcopy(f.server.drive.files)
        preview=f.run_command()
        self.assertEqual(preview['blockers'],[])
        self.assertEqual(preview['counts'],{'READY':1,'COMPLETE':2})
        applied=f.run_command(apply=True)
        self.assertEqual(applied['counts'],{'APPLIED':1},applied)
        self.assertEqual([r['fileId'] for r in f.server.requests],['source-root'])
        for sid in ('leaf','copy'):self.assertEqual(f.server.drive.files[sid],before[sid])

    def test_new_consolidation_collision_remains_blocked(self):
        f=self.fixture();f.server.drive.files['copy']['parents']=['old-parent']
        with self.assertRaisesRegex(SafetyError,'DESTINATION_EXACT_NAME_COLLISION'):f.run_command()
        self.assertEqual(f.server.requests,[])

    def test_new_rename_collision_remains_blocked(self):
        f=self.fixture();f.server.drive.files['copy']['name']='Different.pdf'
        with self.assertRaisesRegex(SafetyError,'DESTINATION_EXACT_NAME_COLLISION'):f.run_command()
        self.assertEqual(f.server.requests,[])


class CreatedAliasTests(unittest.TestCase):
    section='Parent/Collection'

    def fixture(self):
        p=self.section;old=p+'/2023 Submissions';new=p+'/Submissions'
        src=source(rec('M','original','MOVE',old),rec('R','original','RENAME',p,'Submissions'),
                   rec('F','incoming','RENAME',new,'File.pdf'))
        src['records']['R']['row']['Current name + type suffix']='2023 Submissions'
        src['folder_rename_coverage']=[{'Source ID':'original','Verified MIME type':FOLDER,
            'Rename proposal':'R','Current name + type suffix':'2023 Submissions',
            'Proposed name / retained name':'Submissions','Existing proposed target':old}]
        states={'anchor':state('anchor','Parent',[],True),'root':state('root','Collection',['anchor'],True),
                'original':state('original','2023 Submissions',['old'],True),
                'incoming':state('incoming','File.pdf',['old']),
                'created':state('created','Submissions',['root'],True)}
        states['created'].update(trashed=False,appProperties={'uqmcFolderKey':'stable'})
        proof={'logical_key':'folder:'+digest({'section_path':p,'logical_path':new}),
               'registered_logical_path':new,'creation_section_path':p,'name':'Submissions',
               'parent_id':'root','stable_key':'stable'}
        return src,states,proof

    def plan(self,src,states,proof):
        return plan_destination(src,section_path=self.section,metadata_by_id=states,
            anchor_paths={'Parent':'anchor',self.section:'root',self.section+'/Submissions':'created'},
            recovered_folders={'created':proof})

    def test_verified_creation_reused_through_corroborated_rename_alias(self):
        src,states,proof=self.fixture();before=copy.deepcopy((src,states,proof))
        result=self.plan(src,states,proof)
        self.assertEqual(result['destination_paths'][self.section+'/2023 Submissions'],'created')
        self.assertEqual(result['missing_folders'],[])
        self.assertEqual(result['source_container_ids'],['original'])
        self.assertEqual((src,states,proof),before)

    def test_unregistered_creation_cannot_acquire_a_new_alias(self):
        src,states,proof=self.fixture();proof.pop('registered_logical_path')
        with self.assertRaisesRegex(SafetyError,'RECOVERED_DESTINATION_PATH_NO_LONGER_APPROVED'):
            self.plan(src,states,proof)

    def test_alias_does_not_waive_changed_parent_or_marker(self):
        for field,value in [('parents',['elsewhere']),('appProperties',{'uqmcFolderKey':'wrong'})]:
            with self.subTest(field=field):
                src,states,proof=self.fixture();states['created'][field]=value
                with self.assertRaisesRegex(SafetyError,'RECOVERED_DESTINATION_METADATA_CONFLICT'):
                    self.plan(src,states,proof)

if __name__=='__main__':unittest.main()
