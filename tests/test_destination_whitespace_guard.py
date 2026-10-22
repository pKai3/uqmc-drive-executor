"""Whitespace-only destination collisions must not create or adopt a new ID."""
import copy
import unittest

import test_destination_command as destination
import test_folder_creation as creation
from uqmc_recovery.folder_creation import reject_trailing_whitespace_aliases
from uqmc_recovery.journal import Journal
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
