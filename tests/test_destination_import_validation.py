"""Deterministic import failures must precede folder mutations and stay visible."""
import io
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import test_destination_command as fixture
from uqmc_recovery.destination import import_blocked
from uqmc_recovery.destination_console import ConsoleReporter
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import SafetyError


class ImportValidationTests(unittest.TestCase):
    def fixture(self):
        f=fixture.DestinationCommandTests();f.setUp();self.addCleanup(f.temp.cleanup)
        return f

    def target(self, f, name):
        rows=f.export['sheets']['Proposed Renames'];headers=rows[0]
        for row in rows[1:]:
            if row[headers.index('Source ID')]=='leaf':
                row[headers.index('Proposed name + type suffix')]=name

    def test_bad_extension_blocks_before_any_folder_allocation_or_source_mutation(self):
        f=self.fixture();f.mixed();self.target(f,'Approved.png')
        with self.assertRaisesRegex(SafetyError,'SECTION_IMPORT_BLOCKED:UPLOADED_EXTENSION_CHANGED.*RENAME-002'):
            f.run_command(apply=True)
        self.assertEqual(f.server.counter,0)
        self.assertEqual(f.server.created,[]);self.assertEqual(f.server.requests,[])
        events=Journal(f.config['store']).records()
        self.assertFalse(any(r['kind'].startswith('REQUIRED_FOLDER_') for r in events))
        self.assertEqual(f.progress[-2]['blocked_rows'][0]['reason_code'],'UPLOADED_EXTENSION_CHANGED')

    def test_case_only_extension_runs_mixed_destination_end_to_end(self):
        f=self.fixture();f.mixed()
        f.server.drive.files['leaf']['name']='Old.PDF'
        rows=f.export['sheets']['Proposed Renames'];headers=rows[0]
        for row in rows[1:]:
            if row[headers.index('Source ID')]=='leaf':row[headers.index('Current name + type suffix')]='Old.PDF'
        result=f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':1})
        self.assertEqual(f.server.drive.files['leaf']['name'],'Approved.pdf')
        self.assertEqual(len(f.server.created),1)
        self.assertEqual(len(f.server.requests),1)

    def test_null_section_reason_does_not_hide_row_errors(self):
        rows=[{'reference':'RENAME-'+str(i),'state':'BLOCKED','reason_code':'UPLOADED_EXTENSION_CHANGED'} for i in range(13)]
        progress=[]
        with self.assertRaisesRegex(SafetyError,r'UPLOADED_EXTENSION_CHANGED \(13: RENAME-0') as caught:
            import_blocked({'section_reason_code':None,'rows':rows},progress.append)
        self.assertNotIn(':None',str(caught.exception));self.assertEqual(progress[0]['blocked_rows'],rows)
        stream=io.StringIO();ConsoleReporter(stream=stream).progress(progress[0])
        self.assertIn('UPLOADED_EXTENSION_CHANGED',stream.getvalue())
        self.assertIn('RENAME-0',stream.getvalue())

    def test_final_import_error_keeps_section_and_rows(self):
        f=self.fixture()
        failure=SimpleNamespace(plan=None,manifest={'section_reason_code':'SCOPE_MISMATCH',
            'rows':[{'state':'BLOCKED','reference':'RENAME-002','reason_code':'EXACT_TARGET_MISMATCH'}]})
        with patch('uqmc_recovery.section_importer.import_section',return_value=failure):
            with self.assertRaisesRegex(SafetyError,'SCOPE_MISMATCH; EXACT_TARGET_MISMATCH'):
                f.run_command(apply=True)
        self.assertFalse(f.server.requests)

    def test_missing_reason_is_actionable_not_none(self):
        with self.assertRaisesRegex(SafetyError,'SECTION_IMPORT_BLOCKED:NO_EXECUTABLE_PLAN'):
            import_blocked({'section_reason_code':None,'rows':[]},lambda _:None)


if __name__=='__main__':unittest.main()
