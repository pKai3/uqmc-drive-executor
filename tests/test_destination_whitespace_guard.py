"""Whitespace-only destination collisions must not create or adopt a new ID."""
import copy
import unittest

import test_destination_command as destination
import test_folder_creation as creation
from uqmc_recovery.folder_creation import reject_trailing_whitespace_aliases
from uqmc_recovery.journal import Journal
from test_importer import set_cell
from uqmc_recovery.model import SafetyError


class DestinationWhitespaceGuardTests(unittest.TestCase):
    def fixture(self):
        f=destination.DestinationCommandTests(methodName='runTest')
        f.setUp();self.addCleanup(f.doCleanups)
        return f

    def test_trailing_alias_blocks_before_any_migration_or_creation(self):
        f=self.fixture()
        f.server.drive.files['existing']=destination.raw('existing','Skills ',['club'],True)
        before=copy.deepcopy(f.server.drive.files)
        with self.assertRaisesRegex(SafetyError,'DESTINATION_TRAILING_WHITESPACE_CONFLICT:Club/Skills:existing'):
            f.run_command(apply=True)
        self.assertEqual(f.server.drive.files,before)
        self.assertEqual(f.server.requests,[]);self.assertEqual(f.server.created,[])
        self.assertEqual(Journal(f.config['store']).records(),[])

    def test_exact_and_trailing_alias_pair_is_not_silently_resolved(self):
        f=self.fixture()
        f.server.drive.files['exact']=destination.raw('exact','Skills',['club'],True)
        f.server.drive.files['spaced']=destination.raw('spaced','Skills ',['club'],True)
        with self.assertRaisesRegex(SafetyError,'DESTINATION_TRAILING_WHITESPACE_CONFLICT'):
            f.run_command()
        self.assertEqual(f.server.requests,[]);self.assertEqual(f.server.created,[])

    def test_unique_exact_destination_remains_eligible(self):
        f=self.fixture()
        f.server.drive.files['exact']=destination.raw('exact','Skills',['club'],True)
        result=f.run_command()
        self.assertEqual(result['missing_folders'],[])
        self.assertEqual(result['blockers'],[])
        self.assertEqual(f.server.requests,[])

    def test_guard_does_not_trim_or_normalize_unrelated_names(self):
        children=[{'id':'other','name':'Other '},{'id':'leading','name':' Skills'}]
        before=copy.deepcopy(children)
        reject_trailing_whitespace_aliases(children,'Skills')
        self.assertEqual(children,before)
        for name in ('Skills ','Skills\t','Skills\u00a0'):
            with self.subTest(name=name),self.assertRaisesRegex(SafetyError,'DESTINATION_TRAILING_WHITESPACE_CONFLICT'):
                reject_trailing_whitespace_aliases([{'id':'alias','name':name}],'Skills')

    def approved_self_fixture(self, title='Skills', whitespace=' '):
        f=self.fixture()
        section='Club/'+title
        for tab, row, field, value in [
            ('Proposed Moves',1,'Proposed target path',section),
            ('Move Manifest',1,'Exact target path',section),
            ('Proposed Renames',1,'Current name + type suffix',title+whitespace),
            ('Proposed Renames',1,'Proposed name + type suffix',title),
            ('Proposed Renames',2,'Proposed destination folder',section)]:
            set_cell(f.export,tab,row,field,value)
        f.server.drive.files['source-root']['name']=title+whitespace
        f.server.drive.files['source-root']['parents']=['club']
        def run(**kwargs):
            return destination.run_destination(f.config,section,capture_client=f.capture,
                factory=f.server.factory,publisher=f.publisher,report=f.progress.append,**kwargs)
        return f,run

    def test_approved_self_rename_is_general_and_preview_never_mutates(self):
        for title, whitespace in [('Skills',' '),('Film Festival Announcement','  '),
                                 ('Archive','\t'),('Media','\u00a0')]:
            with self.subTest(title=title,whitespace=whitespace):
                f,run=self.approved_self_fixture(title,whitespace)
                before=copy.deepcopy(f.server.drive.files)
                result=run()
                self.assertEqual(result['blockers'],[])
                self.assertEqual(result['whole_folder_reuse_ids'],['source-root'])
                self.assertEqual(result['missing_folders'],[])
                self.assertEqual(f.server.drive.files,before)
                self.assertEqual(f.server.requests,[])
                self.assertEqual(Journal(f.config['store']).records(),[])

    def test_approved_self_rename_apply_keeps_id_parent_and_history(self):
        f,run=self.approved_self_fixture()
        result=run(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2})
        self.assertEqual(f.server.requests[0],{'fileId':'source-root','name':'Skills'})
        self.assertEqual(f.server.drive.files['source-root']['parents'],['club'])
        self.assertEqual(f.server.created,[])
        journal=Journal(f.config['store'])
        self.assertTrue(any(e['kind']=='DESIRED_ATTEMPTED' for e in journal.records()))
        requests=copy.deepcopy(f.server.requests)
        again=run(apply=True)
        self.assertTrue(again['no_op'])
        self.assertEqual(f.server.requests,requests)

    def test_approved_self_rename_cannot_adopt_different_collision_id(self):
        for name,folder in [('Skills',True),('Skills ',True),('Skills\t',True),('Skills',False)]:
            with self.subTest(name=name,folder=folder):
                f,run=self.approved_self_fixture()
                f.server.drive.files['other']=destination.raw('other',name,['club'],folder)
                before=copy.deepcopy(f.server.drive.files)
                with self.assertRaisesRegex(SafetyError,'DESTINATION_TRAILING_WHITESPACE_CONFLICT:.*:other'):
                    run(apply=True)
                self.assertEqual(f.server.drive.files,before)
                self.assertEqual(f.server.requests,[])
                self.assertEqual(f.server.created,[])
                self.assertEqual(Journal(f.config['store']).records(),[])

    def test_self_rename_requires_exact_original_title_and_explicit_proposal(self):
        for mode in ('drift','missing','declined'):
            with self.subTest(mode=mode):
                f,run=self.approved_self_fixture()
                if mode=='drift':f.server.drive.files['source-root']['name']='Skills  '
                if mode=='missing':del f.export['sheets']['Proposed Renames'][1]
                if mode=='declined':set_cell(f.export,'Proposed Renames',1,'Decision (Yes/No)','No')
                with self.assertRaises(SafetyError):run(apply=True)
                self.assertEqual(f.server.requests,[])
                self.assertEqual(f.server.created,[])

    def creation_fixture(self):
        f=creation.FolderCreationTests(methodName='runTest')
        f.setUp();self.addCleanup(f.doCleanups)
        return f

    def test_creation_boundary_blocks_before_id_allocation(self):
        f=self.creation_fixture()
        f.drive.files['existing']={'id':'existing','name':'Section ','parents':['parent']}
        with self.assertRaisesRegex(SafetyError,'DESTINATION_TRAILING_WHITESPACE_CONFLICT'):
            f.run_folder()
        self.assertEqual(f.drive.counter,0)
        self.assertEqual(f.j.records(),[])
        self.assertFalse(any(c[0]=='create' for c in f.drive.calls))

    def test_alias_appearing_after_allocation_blocks_dispatch(self):
        f=self.creation_fixture();original=f.drive.get_reserved_folder
        def read_reserved(file_id):
            f.drive.files['late-alias']={'id':'late-alias','name':'Section ','parents':['parent']}
            return original(file_id)
        f.drive.get_reserved_folder=read_reserved
        with self.assertRaisesRegex(SafetyError,'DESTINATION_TRAILING_WHITESPACE_CONFLICT'):
            f.run_folder()
        self.assertEqual([e['kind'] for e in f.j.records()],['REQUIRED_FOLDER_ALLOCATED'])
        self.assertFalse(any(c[0]=='create' for c in f.drive.calls))


if __name__=='__main__':unittest.main()
