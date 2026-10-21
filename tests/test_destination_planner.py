"""Offline destination hybrid scope and identity proofs."""
import copy
import unittest
from uqmc_recovery.destination_planner import plan_destination, build_hybrid_preparation
from uqmc_recovery.model import FOLDER, SafetyError
import test_section_prepare as fixtures
from test_section_prepare import SECTION, rec, state, source


class HybridPlannerTests(unittest.TestCase):
    def setUp(self):
        base = fixtures.PreparationTests(); base.setUp()
        self.states, self.source = base.states, base.src
        self.anchors = {'Parent': 'anchor'}

    def plan(self, anchors=None):
        return plan_destination(self.source, section_path=SECTION, metadata_by_id=self.states,
                                anchor_paths=self.anchors if anchors is None else anchors)

    def resolve(self):
        layout = self.plan()
        paths = layout['destination_paths']
        for index, spec in enumerate(layout['missing_folders']):
            sid = 'new-' + str(index)
            self.states[sid] = state(sid, spec['folder_name'], [paths[spec['parent_path']]], True)
            paths[spec['logical_path']] = sid
        return paths

    def build(self):
        return build_hybrid_preparation(self.source, section_path=SECTION, section_id='skills',
            workbook_id='fixture', metadata_by_id=self.states, destination_paths=self.resolve(),
            destination_path_evidence_ref='proof')

    def mixed(self):
        self.states['root']['subtree']['members'].append({'id': 'outside', 'name': 'EOI',
                                                        'parents': ['root'], 'mime_type': 'application/pdf'})
        self.source['records']['outside'] = rec('outside', 'outside', 'MOVE', 'Parent/Training/EOI')

    def nested(self):
        member = {'id':'nested', 'name':'Old nested', 'parents':['root'], 'mime_type':FOLDER}
        self.states['root']['subtree']['members'].append(member)
        self.states['nested'] = state('nested', 'Old nested', ['root'], True)
        self.source['records']['Mnested'] = rec('Mnested', 'nested', 'MOVE', SECTION + '/Old nested')
        self.source['records']['Rnested'] = rec('Rnested', 'nested', 'RENAME', SECTION, 'Better nested')

    def test_intact_folder_reuses_identity_not_recreated(self):
        result = self.plan()
        self.assertEqual(result['reused_folder_ids'], ['root'])
        self.assertEqual(result['destination_paths'][SECTION], 'root')
        self.assertEqual([s['logical_path'] for s in result['missing_folders']], [SECTION + '/Year'])
        self.assertEqual(result['folder_reuse_proofs']['root']['descendant_ids'], ['child'])
        self.assertEqual(result, self.plan())

    def test_mixed_source_retained_selected_files_extracted(self):
        self.mixed()
        result = self.build()
        self.assertEqual(result['source_ids'], ['child', 'other'])
        self.assertEqual(result['projection']['source_container_ids'], ['root'])
        self.assertNotIn('outside', self.states)  # No metadata lookup for out-of-scope child.
        self.assertTrue(all(m['ancestor_move_operation_refs'] == [] for m in result['projection']['members']))

    def test_nested_intact_subfolder_reused_inside_mixed_parent(self):
        self.nested(); self.mixed()
        layout = self.plan()
        self.assertEqual(layout['reused_folder_ids'], ['nested'])
        self.assertEqual(layout['source_container_ids'], ['root'])
        self.assertEqual(layout['destination_paths'][SECTION+'/Old nested'], 'nested')
        self.assertEqual(layout['destination_folder_names'][SECTION+'/Old nested'], 'Better nested')
        result = self.build()
        self.assertEqual(set(result['source_ids']), {'nested', 'child', 'other'})
        self.assertNotIn('root', {o['source_id'] for o in result['projection']['operations']})

    def test_intact_nested_folder_dependencies_preserve_stable_ids(self):
        self.nested()
        result = self.build()
        ops = {o['reference']:o for o in result['projection']['operations']}
        self.assertEqual(ops['Rnested']['depends_on_refs'], ['Mroot'])
        self.assertEqual(ops['Mnested']['target_parent_ids'], ['root'])
        self.assertEqual(ops['ROUTE/Rchild']['target_parent_ids'], ['root'])
        self.assertEqual(ops['Rchild']['depends_on_refs'], ['Mroot'])
        self.assertEqual(ops['Rother']['depends_on_refs'], [])

    def test_unassigned_passenger_never_inherits_folder_scope(self):
        self.states['root']['subtree']['members'].append({'id':'unassigned'})
        self.assertEqual(self.plan()['reused_folder_ids'], [])
        self.assertEqual(self.plan()['source_container_reasons']['root'], 'UNASSIGNED_OR_OUTSIDE_SECTION_PASSENGER')

    def test_withdrawn_or_no_or_excluded_descendant_prevents_folder_move(self):
        for mode in ['withdrawn', 'No', 'excluded']:
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'withdrawn': self.source['records']['Rchild']['withdrawn'] = True
                if mode == 'No': self.source['records']['Rchild']['decision'] = 'No'
                if mode == 'excluded': self.source['excluded_ids'] = ['child']
                p = self.build()
                self.assertEqual(p['source_ids'], ['other'])
                self.assertEqual(p['projection']['reused_folder_ids'], [])

    def test_no_move_cannot_be_bypassed_by_derived_route(self):
        self.source['records']['Mchild'] = rec('Mchild', 'child', 'MOVE', SECTION+'/New.pdf')
        self.source['records']['Mchild']['decision'] = 'No'
        p = self.build()
        self.assertNotIn('ROUTE/Rchild', p['selected_refs'])
        self.assertNotIn('child', p['source_ids'])

    def test_assigned_descendant_elsewhere_in_section_prevents_intact_move(self):
        self.source['records']['Rchild']['row']['Proposed destination folder'] = SECTION+'/Year'
        p = self.plan()
        self.assertEqual(p['reused_folder_ids'], [])
        self.assertEqual(p['source_container_reasons']['root'], 'DESCENDANT_ASSIGNED_TO_DIFFERENT_ROUTE')

    def test_incomplete_subtree_never_reused(self):
        self.states['root']['subtree']['complete'] = False
        self.assertEqual(self.plan()['reused_folder_ids'], [])
        self.assertEqual(self.build()['source_ids'], ['child', 'other'])

    def test_subtree_known_metadata_conflict_rejected(self):
        self.states['root']['subtree']['members'][0]['name'] = 'Unexpected.pdf'
        with self.assertRaisesRegex(SafetyError, 'MEMBERSHIP_METADATA_CONFLICT'):
            self.plan()

    def test_existing_destination_preferred_over_reusing_source_container(self):
        self.states['dest'] = state('dest', 'Skills', ['anchor'], True)
        p = self.plan({'Parent':'anchor', SECTION:'dest'})
        self.assertEqual(p['reused_folder_ids'], [])
        self.assertEqual(p['source_container_ids'], ['root'])
        self.assertEqual(p['destination_paths'][SECTION], 'dest')

    def test_mixed_source_cannot_be_smuggled_into_anchor_map(self):
        self.mixed()
        with self.assertRaisesRegex(SafetyError, 'MIXED_SOURCE_CANNOT'):
            self.plan({'Parent':'anchor',SECTION:'root'})

    def test_two_intact_source_folders_for_same_path_are_ambiguous(self):
        self.states['root2'] = state('root2', 'Other source', ['old'], True)
        self.source['records']['Mroot2'] = rec('Mroot2','root2','MOVE',SECTION)
        with self.assertRaisesRegex(SafetyError, 'AMBIGUOUS_REUSABLE_FOLDERS'):
            self.plan()

    def test_final_file_exact_name_collision_rejected(self):
        self.source['records']['Rother']['row']['Proposed destination folder'] = SECTION
        self.source['records']['Mother']['row']['Exact target path'] = SECTION+'/New.pdf'
        self.source['records']['Rother']['row']['Proposed name + type suffix'] = 'New.pdf'
        with self.assertRaisesRegex(SafetyError, 'EXACT_NAME_COLLISION'):
            self.plan()

    def test_file_and_folder_name_collision_rejected(self):
        self.source['records']['Rchild']['row']['Proposed name + type suffix'] = 'Year'
        with self.assertRaisesRegex(SafetyError, 'EXACT_NAME_COLLISION'):
            self.plan()

    def test_build_requires_resolved_destinations_and_does_not_mutate_input(self):
        before = copy.deepcopy((self.states, self.source))
        with self.assertRaisesRegex(SafetyError, 'CREATION_OR_MAPPING_REQUIRED'):
            build_hybrid_preparation(self.source, section_path=SECTION, section_id='skills', workbook_id='fixture',
                metadata_by_id=self.states, destination_paths=self.anchors, destination_path_evidence_ref='proof')
        self.assertEqual((self.states,self.source), before)

    def test_completed_reused_root_is_still_same_destination_id(self):
        self.states['root']['name'] = 'Skills'
        self.states['root']['parents'] = ['anchor']
        p = self.plan({'Parent':'anchor', SECTION:'root'})
        self.assertEqual(p['reused_folder_ids'], ['root'])
        self.assertEqual(p['destination_paths'][SECTION], 'root')

    def test_existing_descendant_destination_prevents_passenger_source_folder_reuse(self):
        self.nested()
        self.states['dest-child'] = state('dest-child','Better nested',['root'],True)
        p = self.plan({'Parent':'anchor',SECTION+'/Old nested':'dest-child'})
        self.assertEqual(p['reused_folder_ids'], [])
        self.assertEqual(p['source_container_reasons']['root'], 'DESCENDANT_FOLDER_REBOUND_TO_OTHER_ID')

    def test_completed_rename_only_folder_keeps_historical_logical_alias(self):
        self.nested()
        del self.source['records']['Mnested']
        rename = self.source['records']['Rnested']['row']
        rename['Current name + type suffix'] = 'Old nested'
        self.states['nested']['name'] = 'Better nested'
        self.states['root']['subtree']['members'][-1]['name'] = 'Better nested'
        self.source['records']['Mother']['row']['Exact target path'] = SECTION+'/Old nested/Other.pdf'
        self.source['records']['Rother']['row']['Proposed destination folder'] = SECTION+'/Old nested'
        p=self.plan()
        self.assertEqual(p['destination_paths'][SECTION+'/Old nested'],'nested')
        self.assertNotIn(SECTION+'/Better nested',p['destination_paths'])

    def test_live_subtree_cannot_omit_known_direct_child(self):
        self.states['root']['subtree']['members'] = []
        with self.assertRaisesRegex(SafetyError,'LIVE_SUBTREE_OMITS_KNOWN_CHILD'):
            self.plan()

    def test_top_level_uses_only_explicit_empty_root_anchor(self):
        self.source = source(rec('Mroot','root','MOVE','Skills'),
            rec('Rroot','root','RENAME','','Skills'),
            rec('Rchild','child','RENAME','Skills','New.pdf'))
        p=plan_destination(self.source,section_path='Skills',metadata_by_id=self.states,anchor_paths={'':'anchor'})
        self.assertEqual(p['destination_paths'],{'':'anchor','Skills':'root'})
        built=build_hybrid_preparation(self.source,section_path='Skills',section_id='skills',workbook_id='fixture',
            metadata_by_id=self.states,destination_paths=p['destination_paths'],destination_path_evidence_ref='proof')
        ops={o['reference']:o for o in built['projection']['operations']}
        self.assertEqual(ops['Mroot']['target_parent_ids'],['anchor'])
        self.assertEqual(ops['Rroot']['exact_target_name'],'Skills')
        self.assertEqual(ops['ROUTE/Rchild']['target_parent_ids'],['root'])
        with self.assertRaisesRegex(SafetyError,'ANCHOR'):
            plan_destination(self.source,section_path='Skills',metadata_by_id=self.states,anchor_paths={'Parent':'anchor'})

    def test_top_level_blank_rename_route_needs_explicit_root_move(self):
        self.source=source(rec('Rroot','root','RENAME','','Skills'),rec('Rchild','child','RENAME','Skills','New.pdf'))
        with self.assertRaisesRegex(SafetyError,'TOP_LEVEL_FOLDER_RENAME_REQUIRES_EXPLICIT_MOVE_ROW'):
            plan_destination(self.source,section_path='Skills',metadata_by_id=self.states,anchor_paths={'':'anchor'})

    def test_literal_date_slashes_in_original_title_are_not_destination_folders(self):
        title='Certificate (Expires 9/1/24), First Aid (Expires 9/1/26)'
        self.source['records']['Mother']['row'].update({'Exact target path':SECTION+'/Year/'+title,'Current item':title})
        self.source['records']['Rother']['row']['Current name + type suffix']=title
        # The file was renamed in an earlier run; the manifest still carries its original exact label.
        self.states['other']['name']='New Other.pdf'
        p=self.plan()
        self.assertEqual(p['item_routes']['other']['parent_path'],SECTION+'/Year')
        self.assertEqual([x['logical_path'] for x in p['missing_folders']],[SECTION+'/Year'])
        self.assertEqual(self.build()['resolutions']['Mother']['target_parent_ids'],['new-0'])

    def test_literal_title_suffix_does_not_override_real_parent_disagreement(self):
        title='Original 9/1/26.pdf'
        self.source['records']['Mother']['row'].update({'Exact target path':SECTION+'/Wrong/'+title,'Current item':title})
        self.source['records']['Rother']['row']['Current name + type suffix']=title
        with self.assertRaisesRegex(SafetyError,'FILE_MOVE_RENAME_DESTINATION_DISAGREEMENT'):
            self.plan()

    def test_ambiguous_current_and_proposed_literal_title_suffix_fails(self):
        self.source['records']['Mother']['row']['Exact target path']=SECTION+'/Year/Prefix/New.pdf'
        self.states['other']['name']='Prefix/New.pdf'
        self.source['records']['Rother']['row']['Proposed name + type suffix']='New.pdf'
        with self.assertRaisesRegex(SafetyError,'AMBIGUOUS_FILE_TITLE_PATH'):
            self.plan()

    def test_exact_original_title_resolves_move_only_file(self):
        del self.source['records']['Rother']
        self.states['other']['name']='Certificate 9/1/26.pdf'
        self.source['records']['Mother']['row'].update({'Exact target path':SECTION+'/Year/Certificate 9/1/26.pdf',
                                                      'Current item':'Certificate 9/1/26.pdf'})
        self.assertEqual(self.plan()['item_routes']['other']['parent_path'],SECTION+'/Year')

    def test_unused_registered_destination_inside_section_is_not_a_target(self):
        expected=self.plan()
        anchors={'Parent':'anchor',SECTION+'/Old empty archive':'unused-folder'}
        # No metadata is needed: this registered identity is neither a source nor a destination of this batch.
        actual=self.plan(anchors)
        self.assertEqual(actual,expected)
        self.assertNotIn('unused-folder',actual['destination_paths'].values())
        self.assertEqual(anchors[SECTION+'/Old empty archive'],'unused-folder')
        paths=self.resolve();paths[SECTION+'/Old empty archive']='unused-folder'
        built=build_hybrid_preparation(self.source,section_path=SECTION,section_id='skills',workbook_id='fixture',
            metadata_by_id=self.states,destination_paths=paths,destination_path_evidence_ref='proof')
        self.assertNotIn('unused-folder',built['projection']['destination_paths'].values())
        self.assertNotIn('unused-folder',{o['source_id'] for o in built['projection']['operations']})

    def test_unused_external_registered_path_is_still_rejected(self):
        with self.assertRaisesRegex(SafetyError,'DESTINATION_ANCHOR_OUTSIDE_SECTION_OR_MISSING'):
            self.plan({'Parent':'anchor','Parent/Unrelated':'unrelated-folder'})

    def test_native_suffix_conversion_requires_bound_proof(self):
        self.states['child']['mime_type'] = 'application/vnd.google-apps.document'
        self.states['root']['subtree']['members'][0]['mime_type'] = self.states['child']['mime_type']
        self.source['records']['Rchild']['row']['Proposed name + type suffix'] = 'New.gdoc'
        paths = self.resolve()
        kwargs=dict(section_path=SECTION, section_id='skills', workbook_id='fixture', metadata_by_id=self.states,
                    destination_paths=paths, destination_path_evidence_ref='proof')
        with self.assertRaisesRegex(SafetyError, 'VERIFIED_NATIVE'):
            build_hybrid_preparation(self.source, **kwargs)
        result=build_hybrid_preparation(self.source, native_conversion_refs={'Rchild':'conversion-proof'}, **kwargs)
        self.assertEqual(result['resolutions']['Rchild']['exact_target_name'],'New')

if __name__ == '__main__': unittest.main()
