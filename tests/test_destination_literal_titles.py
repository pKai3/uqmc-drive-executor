"""Literal title separators never become destination hierarchy (offline only)."""
import copy
import unittest

import test_destination_command as command_tests
from test_destination_command import SECTION
import test_destination_planner as planner_tests
from test_importer import append_row
from test_section_prepare import rec
from uqmc_recovery.destination_planner import file_parent_path, select_destination_records
from uqmc_recovery.importer import HEADERS
from uqmc_recovery.model import SafetyError
from uqmc_recovery.section_prepare import select_records


class LiteralTitleTests(unittest.TestCase):
    def route(self, title, *, native=False, parent='Club/Meetings '):
        label=title+'.gdoc' if native else title
        move=rec('move','item','MOVE',parent+'/'+title)
        move['row']['Current item']=label
        rename=rec('rename','item','RENAME',parent,'Approved.gdoc' if native else 'Approved.pdf')
        rename['row']['Current name + type suffix']=label
        state={'id':'item','name':title,'mime_type':'application/vnd.google-apps.document' if native else 'application/pdf'}
        return move,rename,state

    def test_opaque_native_titles_and_whitespace_are_preserved(self):
        for title in ['Meeting 5 - 5/3//18', 'Meeting // ', 'Meeting/', '/Meeting', 'Meeting/../18', 'Meeting  ']:
            with self.subTest(title=title):
                move,rename,state=self.route(title,native=True)
                original=copy.deepcopy((move,rename,state))
                self.assertEqual(file_parent_path(move,rename,state),'Club/Meetings ')
                self.assertEqual((move,rename,state),original)

    def test_move_only_exact_label_preserves_binary_title(self):
        move,_,state=self.route('minutes//part /final.pdf ')
        self.assertEqual(file_parent_path(move,None,state),'Club/Meetings ')

    def test_history_native_display_label_resolves_after_previous_rename(self):
        move,rename,state=self.route('Meeting 5 - 5/3//18',native=True)
        state['name']='Approved'
        self.assertEqual(file_parent_path(move,rename,state),'Club/Meetings ')

    def test_real_parent_empty_segment_remains_blocked(self):
        move,rename,state=self.route('Meeting//18',native=True,parent='Club//Meetings')
        with self.assertRaisesRegex(SafetyError,'AMBIGUOUS_LOGICAL_PATH'):
            file_parent_path(move,rename,state)

    def test_conflicting_same_id_destination_remains_blocked(self):
        move,rename,state=self.route('Meeting//18',native=True)
        rename['row']['Proposed destination folder']='Club/Other'
        with self.assertRaisesRegex(SafetyError,'FILE_MOVE_RENAME_DESTINATION_DISAGREEMENT'):
            file_parent_path(move,rename,state)

    def test_unproven_title_boundary_is_never_guessed(self):
        move,rename,state=self.route('Meeting//18',native=True)
        move['row']['Exact target path']='Club/Meetings /Unknown/Title'
        with self.assertRaisesRegex(SafetyError,'FILE_TITLE_PATH_EVIDENCE_REQUIRED'):
            file_parent_path(move,rename,state)

    def test_native_display_suffix_is_not_removed_for_uploaded_binary(self):
        move,rename,state=self.route('Meeting//18.gdoc')
        move['row']['Exact target path']='Club/Meetings /Meeting//18'
        with self.assertRaisesRegex(SafetyError,'FILE_TITLE_PATH_EVIDENCE_REQUIRED'):
            file_parent_path(move,rename,state)

    def test_hybrid_defers_title_only_legacy_selection_stays_strict(self):
        move,rename,state=self.route('Meeting//18',native=True)
        source={'records':{'move':move,'rename':rename}}
        self.assertEqual(len(select_destination_records(source,'Club/Meetings ')),2)
        with self.assertRaisesRegex(SafetyError,'AMBIGUOUS_LOGICAL_PATH'):
            select_records(source,'Club/Meetings ')

    def test_folder_path_validation_is_not_relaxed(self):
        f=planner_tests.HybridPlannerTests();f.setUp()
        f.source['records']['Mroot']['row']['Exact target path']='Parent/Skills//Bad'
        with self.assertRaisesRegex(SafetyError,'AMBIGUOUS_LOGICAL_PATH'):
            f.plan()

    def test_fake_cli_native_title_import_apply_and_completed_resume(self):
        f=command_tests.DestinationCommandTests();f.setUp();self.addCleanup(f.doCleanups)
        original='Meeting 5 - 5/3//18 '
        approved='Committee Meeting 5 '
        value=f.server.drive.files['leaf']
        value.update(name=original,mimeType='application/vnd.google-apps.document')
        headers=HEADERS['Proposed Renames'];row=f.export['sheets']['Proposed Renames'][2]
        row[headers.index('Current name + type suffix')]=original+'.gdoc'
        row[headers.index('Proposed name + type suffix')]=approved+'.gdoc'
        append_row(f.export,'Proposed Moves',{'Proposal ID':'MOVE-LEAF','Action':'Move','Proposed target path':SECTION+'/'+original})
        append_row(f.export,'Move Manifest',{'Proposal ID':'MOVE-LEAF','Manifest item':'ITEM-LEAF',
            'Source ID':'leaf','Current item':original+'.gdoc','Exact target path':SECTION+'/'+original,'Scope':'Single item'})
        result=f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(f.server.created,[])
        self.assertEqual(f.server.requests[-1],{'fileId':'leaf','name':approved})
        self.assertEqual(f.server.drive.files['leaf']['parents'],['source-root'])
        previous=copy.deepcopy(f.server.requests)
        preview=f.run_command()
        self.assertEqual(preview['counts'],{'COMPLETE':2},preview)
        self.assertEqual(f.server.requests,previous)
        self.assertEqual(preview['missing_folders'],[])


if __name__=='__main__': unittest.main()
