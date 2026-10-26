"""A slash inside a verified folder title never creates phantom ancestors."""
import copy
import unittest

import test_destination_command as fixture
from test_importer import append_row
from uqmc_recovery.model import SafetyError


class OpaqueFolderTests(unittest.TestCase):
    title='Past (2020/2021/2022) Certs'

    def fixture(self,rename_row=False,child_destination=False):
        f=fixture.DestinationCommandTests();f.setUp();self.addCleanup(f.temp.cleanup)
        path=fixture.SECTION+'/'+self.title
        f.server.drive.files['past']=fixture.raw('past',self.title,['source-root'],True)
        f.server.drive.files['leaf']['parents']=['past']
        rows=f.export['sheets']['Proposed Renames'];heads=rows[0]
        rows[2][heads.index('Proposed destination folder')]=path
        append_row(f.export,'Proposed Moves',{'Proposal ID':'MOVE-PAST','Action':'Move','Proposed target path':path})
        append_row(f.export,'Move Manifest',{'Proposal ID':'MOVE-PAST','Manifest item':'ITEM-PAST',
            'Source ID':'past','Current item':self.title,'Exact target path':path,
            'Scope':'Whole subtree including all descendants'})
        if rename_row:
            append_row(f.export,'Proposed Renames',{'Rename ID':'RENAME-PAST','Source ID':'past',
                'Current name + type suffix':self.title,'Proposed name + type suffix':self.title,
                'Proposed destination folder':fixture.SECTION})
        if child_destination:
            f.server.drive.files['incoming']=fixture.raw('incoming','Incoming.pdf',['old-parent'])
            append_row(f.export,'Proposed Renames',{'Rename ID':'RENAME-INCOMING','Source ID':'incoming',
                'Current name + type suffix':'Incoming.pdf','Proposed name + type suffix':'Incoming.pdf',
                'Proposed destination folder':path+'/Receipts'})
        return f,path

    def test_move_only_opaque_folder_reuses_exact_id_and_does_not_split_name(self):
        f,path=self.fixture();before=copy.deepcopy(f.server.drive.files['past'])
        result=f.run_command()
        self.assertEqual(result['missing_folders'],[])
        self.assertEqual(result['blockers'],[])
        self.assertIn('past',result['whole_folder_reuse_ids'])
        result=f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(f.server.created,[])
        self.assertEqual(f.server.drive.files['past'],before)
        self.assertEqual(set(x['fileId'] for x in f.server.requests),{'leaf','source-root'})

    def test_keep_name_row_and_created_child_use_full_folder_parent(self):
        f,path=self.fixture(rename_row=True,child_destination=True)
        preview=f.run_command()
        self.assertEqual(preview['missing_folders'],[path+'/Receipts'])
        self.assertEqual(preview['blockers'],[])
        applied=f.run_command(apply=True)
        self.assertEqual(applied['counts'],{'APPLIED':3},applied)
        self.assertEqual(len(f.server.created),1)
        created=f.server.drive.files[f.server.created[0]]
        self.assertEqual((created['name'],created['parents']),('Receipts',['past']))
        self.assertEqual(f.server.drive.files['past']['name'],self.title)
        self.assertNotIn('past',[x['fileId'] for x in f.server.requests])
        again=f.run_command(apply=True)
        self.assertTrue(again.get('no_op'),again)
        self.assertEqual(len(f.server.created),1)

    def test_contradictory_structured_rename_parent_is_blocked(self):
        f,path=self.fixture(rename_row=True)
        rows=f.export['sheets']['Proposed Renames'];heads=rows[0]
        rows[-1][heads.index('Proposed destination folder')]=fixture.SECTION+'/Elsewhere'
        with self.assertRaisesRegex(SafetyError,'FOLDER_MOVE_RENAME_PARENT_DISAGREEMENT'):
            f.run_command()
        self.assertEqual(f.server.requests,[])

if __name__=='__main__':unittest.main()
