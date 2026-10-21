"""Destination section compilation never reparents mixed source containers."""
import copy
import unittest
import test_section_prepare as fixtures
from test_section_prepare import SECTION, rec, state
from uqmc_recovery.model import SafetyError
from uqmc_recovery.section_prepare import resolve_destination_paths, build_preparation, select_records

PROFILE='destination_scoped_files'

class DestinationPreparationTests(unittest.TestCase):
    def setUp(self):
        base=fixtures.PreparationTests();base.setUp()
        self.states,self.source=base.states,base.src
        # The source root includes a form assigned to another destination.
        self.states['root']['subtree']['members'].append({'id':'unrelated-eoi','name':'EOI Responses','parents':['root'],'mime_type':'application/vnd.google-apps.spreadsheet'})
        self.source['records']['outside']=rec('outside','unrelated-eoi','MOVE','Parent/Training/EOI Responses')
        self.states['destination']=state('destination','Skills',['anchor'],True)
        self.states['destination-year']=state('destination-year','Year',['destination'],True)
        self.paths={'Parent':'anchor',SECTION:'destination',SECTION+'/Year':'destination-year'}

    def build(self):
        return build_preparation(self.source,section_path=SECTION,section_id='skills',workbook_id='fixture',
            metadata_by_id=self.states,destination_paths=self.paths,destination_path_evidence_ref='proof',
            method_profile=PROFILE)

    def test_folders_become_required_destination_containers_not_source_moves(self):
        result=resolve_destination_paths(select_records(self.source,SECTION),self.states,{'Parent':'anchor'},SECTION,method_profile=PROFILE)
        self.assertEqual(result['destination_paths'],{'Parent':'anchor'})
        self.assertEqual([x['logical_path'] for x in result['missing_folders']],[SECTION,SECTION+'/Year'])
        self.assertEqual(result['source_container_ids'],['root'])
        self.assertEqual(result['source_container_refs'],['Mroot','Rroot'])
        self.assertEqual(result['destination_folder_names'],{SECTION:'Skills',SECTION+'/Year':'Year'})

    def test_mixed_source_unrelated_child_and_container_are_never_mutations(self):
        before=copy.deepcopy(self.states)
        result=self.build();p=result['projection']
        self.assertEqual(result['source_ids'],['child','other'])
        self.assertEqual({o['source_id'] for o in p['operations']},{'child','other'})
        self.assertEqual({m['source_id'] for m in p['members']},{'child','other'})
        self.assertTrue(all(m['ancestor_move_operation_refs']==[] for m in p['members']))
        self.assertEqual(self.states,before)
        self.assertNotIn('unrelated-eoi',self.states) # No metadata lookup required.

    def test_rename_only_assignment_derives_exact_routing_record(self):
        result=self.build();ops={x['reference']:x for x in result['projection']['operations']}
        route=ops['ROUTE/Rchild']
        self.assertEqual(route['derived_from_reference'],'Rchild')
        self.assertEqual(route['target_parent_ids'],['destination'])
        self.assertEqual(route['depends_on_refs'],['Rchild'])
        self.assertEqual(ops['Mother']['target_parent_ids'],['destination-year'])
        self.assertNotIn('ROUTE/Rother',ops)
        self.assertNotEqual(result['resolutions']['ROUTE/Rchild']['row_sha256'],self.source['records']['Rchild']['row_sha256'])

    def test_only_own_effects_ordered_no_global_phase(self):
        ops={o['reference']:o for o in self.build()['projection']['operations']}
        self.assertEqual(ops['Rchild']['depends_on_refs'],[])
        self.assertEqual(ops['Rother']['depends_on_refs'],[])
        self.assertEqual(ops['Mother']['depends_on_refs'],['Rother'])

    def test_selected_source_folder_cannot_be_adopted_as_destination(self):
        self.paths[SECTION]='root'
        with self.assertRaisesRegex(SafetyError,'SOURCE_CONTAINER_CANNOT_BE_DESTINATION'):
            self.build()

    def test_withdrawn_file_stays_excluded(self):
        self.source['excluded_ids']=['child']
        result=self.build()
        self.assertEqual(result['source_ids'],['other'])
        self.assertNotIn('ROUTE/Rchild',result['selected_refs'])

    def test_conflicting_destination_rows_fail_closed(self):
        self.source['records']['Rother']['row']['Proposed destination folder']=SECTION
        with self.assertRaisesRegex(SafetyError,'FILE_MOVE_RENAME_DESTINATION_DISAGREEMENT'):
            self.build()

    def test_multiple_old_containers_can_share_one_approved_destination(self):
        self.states['second-root']=state('second-root','Another old source',['old'],True)
        self.source['records']['Msecond']=rec('Msecond','second-root','MOVE',SECTION)
        self.source['records']['Rsecond']=rec('Rsecond','second-root','RENAME','Parent','Skills')
        result=resolve_destination_paths(select_records(self.source,SECTION),self.states,{'Parent':'anchor'},SECTION,method_profile=PROFILE)
        self.assertEqual(result['source_container_ids'],['root','second-root'])
        self.assertEqual(sum(s['logical_path']==SECTION for s in result['missing_folders']),1)
        self.assertEqual(result['destination_folder_names'][SECTION],'Skills')

    def test_logical_old_label_uses_exact_proposed_new_folder_title(self):
        self.states['child']=state('child','Old collection',['root'],True)
        self.source['records']['Rchild']['row']['Proposed name + type suffix']='Better Collection'
        self.source['records']['Mchild']=rec('Mchild','child','MOVE',SECTION+'/Old collection')
        result=resolve_destination_paths(select_records(self.source,SECTION),self.states,{'Parent':'anchor'},SECTION,method_profile=PROFILE)
        spec=next(x for x in result['missing_folders'] if x['logical_path']==SECTION+'/Old collection')
        self.assertEqual(spec['folder_name'],'Better Collection')
        self.assertNotIn(SECTION+'/Better Collection',result['destination_folder_names'])

if __name__=='__main__':unittest.main()
