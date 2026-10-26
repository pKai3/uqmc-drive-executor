"""Destination scaffold IDs are context, not new migration operations."""
import copy
import unittest
from unittest.mock import patch

import test_destination_command as fixtures
from test_importer import append_row
from test_sheets_capture import response,literal
from uqmc_recovery.destination import read_and_plan
from uqmc_recovery.destination_mapping import destination_mappings
from uqmc_recovery.importer import OPTIONAL_HEADERS,workbook_fingerprint,catalog
from uqmc_recovery.model import SafetyError
from uqmc_recovery.sheets_capture import normalize_capture

TAB='Destination Folder Names'
HEAD=OPTIONAL_HEADERS[TAB]
SECTION=fixtures.SECTION


class DestinationMappingTests(unittest.TestCase):
    setUp=fixtures.DestinationCommandTests.setUp
    publisher=fixtures.DestinationCommandTests.publisher
    run_command=fixtures.DestinationCommandTests.run_command

    def mapped(self,*,name='Legacy / Skills',path=SECTION,sid='destination'):
        self.export['sheets'].setdefault(TAB,[HEAD.copy()])
        self.export['complete'][TAB]=True
        values={'Proposal ID':'MAP-'+sid,'Source folder ID':sid,
            'Current folder name':name,'Proposed folder name':path.rsplit('/',1)[-1],
            'Destination folder path':path}
        self.export['sheets'][TAB].append([values.get(h,'') for h in HEAD])
        parent='club' if path==SECTION else 'destination'
        self.server.drive.files[sid]=fixtures.raw(sid,name,[parent],True)

    def test_preview_reuses_id_with_literal_slash_name_and_never_mutates(self):
        self.mapped();before=copy.deepcopy(self.server.drive.files)
        result=self.run_command()
        self.assertEqual(result['missing_folders'],[])
        self.assertEqual(result['existing_destination_paths'],{SECTION:'destination'})
        self.assertEqual(result['retained_source_folder_ids'],['source-root'])
        self.assertEqual(result['mapped_destination_folders'][SECTION]['observed_name'],'Legacy / Skills')
        self.assertEqual(self.server.drive.files,before)
        self.assertEqual(self.server.requests,[]);self.assertEqual(self.server.created,[])

    def test_apply_fixture_moves_leaf_into_verified_id_without_renaming_context(self):
        self.mapped();before=copy.deepcopy(self.server.drive.files['destination'])
        result=self.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':1})
        self.assertEqual(self.server.drive.files['destination'],before)
        self.assertEqual(self.server.drive.files['leaf']['parents'],['destination'])
        self.assertEqual(self.server.created,[])
        self.assertEqual([r['fileId'] for r in self.server.requests],['leaf'])

    def test_nested_numbered_alias_and_slash_basename_reuse_by_parent_id(self):
        self.mapped(name='0 Old Skills')
        self.mapped(name='1 Current in/force',path=SECTION+'/Current',sid='current')
        row=self.export['sheets']['Proposed Renames'][2]
        row[self.export['sheets']['Proposed Renames'][0].index('Proposed destination folder')]=SECTION+'/Current'
        result=self.run_command()
        self.assertEqual(result['missing_folders'],[])
        self.assertEqual(result['existing_destination_paths'][SECTION+'/Current'],'current')
        self.assertEqual(len(result['mapped_destination_folders']),2)

    def test_selecting_a_subtree_reuses_renamed_ancestor_by_id(self):
        from uqmc_recovery.destination import run_destination
        self.mapped(name='0 Old Skills')
        self.mapped(name='1 Current in/force',path=SECTION+'/Current',sid='current')
        row=self.export['sheets']['Proposed Renames'][2]
        row[self.export['sheets']['Proposed Renames'][0].index('Proposed destination folder')]=SECTION+'/Current'
        result=run_destination(self.config,SECTION+'/Current',capture_client=self.capture,
            factory=self.server.factory,publisher=self.publisher,report=self.progress.append)
        self.assertEqual(result['missing_folders'],[])
        self.assertEqual(result['counts'],{'READY':1})
        self.assertEqual(self.server.requests,[])

    def test_real_name_parent_and_different_id_collisions_are_blocked(self):
        for defect,reason in [('name','NAME_DRIFT'),('parent','PARENT_DRIFT'),('collision','NAME_COLLISION')]:
            with self.subTest(defect=defect):
                self.setUp();self.mapped()
                if defect=='name':self.server.drive.files['destination']['name']='Unrelated edit'
                if defect=='parent':self.server.drive.files['destination']['parents']=['old-parent']
                if defect=='collision':self.server.drive.files['duplicate']=fixtures.raw('duplicate','Skills',['club'],True)
                with self.assertRaisesRegex(SafetyError,'DESTINATION_MAPPING_'+reason):self.run_command()
                self.assertEqual(self.server.requests,[]);self.assertEqual(self.server.created,[])

    def test_withdrawal_does_not_fall_back_to_duplicate_creation(self):
        self.mapped()
        self.export['sheets'][TAB][1][HEAD.index('Decision (Yes/No)')]='No'
        with self.assertRaisesRegex(SafetyError,'DESTINATION_MAPPING_WITHDRAWN'):self.run_command()
        self.assertEqual(self.server.created,[])

    def test_mapping_is_frozen_in_hash_and_no_extra_execution_records(self):
        base=self.capture.capture();old=catalog(base)
        self.mapped();new=self.capture.capture();new_catalog=catalog(new)
        self.assertNotEqual(old['input_sha256'],new_catalog['input_sha256'])
        self.assertEqual(old['records'],new_catalog['records'])
        new['sheets'][TAB][1][HEAD.index('Source folder ID')]='another-id'
        self.assertNotEqual(new_catalog['input_sha256'],workbook_fingerprint(new))

    def test_optional_mapping_tab_preserves_old_frozen_capture(self):
        first=self.capture.capture()
        self.assertEqual(catalog(first)['input_sha256'],workbook_fingerprint(first))
        self.assertEqual(catalog(first)['destination_folder_mappings'],{})

    def test_historical_trailing_space_already_removed_does_not_create_duplicate(self):
        self.mapped(name='0 Old Skills ')
        self.server.drive.files['destination']['name']='0 Old Skills'
        result=self.run_command()
        self.assertFalse(result['missing_folders'])
        self.assertEqual(result['mapped_destination_folders'][SECTION]['observed_name'],'0 Old Skills')
        self.assertEqual(self.server.requests,[])

    def test_completed_target_name_already_present_is_accepted(self):
        self.mapped();self.server.drive.files['destination']['name']='Skills'
        result=self.run_command()
        self.assertFalse(result['missing_folders']);self.assertFalse(result['blockers'])

    def test_duplicate_mapping_fails_closed(self):
        self.mapped();self.export['sheets'][TAB].append(self.export['sheets'][TAB][1].copy())
        with self.assertRaisesRegex(SafetyError,'DESTINATION_MAPPING_DUPLICATE'):self.run_command()


class MappingCaptureTests(unittest.TestCase):
    def test_native_capture_keeps_mapping_values_and_evidence(self):
        data=response()
        data['sheets'].append({'properties':{'sheetId':99,'title':TAB,
            'gridProperties':{'rowCount':20,'columnCount':10}},
            'data':[{'rowData':[{'values':[literal(v) for v in HEAD]}]}]})
        value=normalize_capture(data,workbook_id='fixture-workbook',revision={'version':'1'},
            started_at_utc='2026-10-26T00:00:00Z',captured_at_utc='2026-10-26T00:00:01Z')
        self.assertEqual(value['sheets'][TAB],[HEAD])
        self.assertTrue(value['complete'][TAB]);self.assertIn(TAB,value['cell_evidence'])
        self.assertEqual(value['input_sha256'],workbook_fingerprint(value))

if __name__=='__main__':unittest.main()
