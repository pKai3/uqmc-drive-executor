import copy
import unittest
from uqmc_recovery.agent_profile import agent_snapshot, validate_agent_plan
from uqmc_recovery.model import FOLDER, Operation, Plan, SafetyError
from uqmc_recovery.section_prepare import build_preparation, resolve_destination_paths, select_records

SECTION = 'Parent/Skills'

def state(sid, name, parents, folder=False, members=()):
    value = {'id': sid, 'name': name, 'parents': parents, 'parents_complete': True,
             'mime_type': FOLDER if folder else 'application/pdf'}
    if folder:
        value['subtree'] = {'complete': True, 'members': list(members)}
    return agent_snapshot(value)

def rec(ref, sid, action, path, name=None, scope='Single item'):
    row = {'Exact target path': path, 'Scope': scope} if action == 'MOVE' else {'Proposed destination folder': path, 'Proposed name + type suffix': name}
    return {'reference': ref, 'source_id': sid, 'action': action, 'row': row, 'row_sha256': 'row-' + ref,
            'proposal_refs': [ref], 'decision': '', 'withdrawn': False}

def source(*records):
    return {'input_sha256': 'fixture-input', 'records': {r['reference']: r for r in records}, 'excluded_ids': [], 'excluded_refs': []}

class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.states = {'anchor': state('anchor', 'Parent', [], True),
            'root': state('root', 'Old', ['old'], True, [{'id':'child','name':'Old.pdf','parents':['root'],'mime_type':'application/pdf'}]),
            'child': state('child', 'Old.pdf', ['root']),
            'other': state('other', 'Other.pdf', ['old']),
            'old':state('old','Old parent',[],True)}
        self.src=source(rec('Mroot','root','MOVE',SECTION,scope='Whole subtree including all descendants'),
            rec('Rroot','root','RENAME','Parent','Skills'),
            rec('Rchild','child','RENAME',SECTION,'New.pdf'),
            rec('Mother','other','MOVE',SECTION+'/Year/Other.pdf'),
            rec('Rother','other','RENAME',SECTION+'/Year','New Other.pdf'))

    def test_exact_prefix_excludes_adjacent_and_withdrawn(self):
        self.src['records']['outside']=rec('outside','x','MOVE','Parent/Skills Extra/A')
        self.src['records']['unrelated-invalid']=rec('unrelated-invalid','z','MOVE','Unrelated//Ambiguous')
        self.src['records']['no']=rec('no','excluded','MOVE',SECTION+'/A')
        self.src['records']['no']['decision']='No'
        self.assertEqual(len(select_records(self.src,SECTION)),5)
        self.src['excluded_ids']=['child']
        self.assertNotIn('Rchild',{r['reference'] for r in select_records(self.src,SECTION)})

    def test_paths_reuse_exact_folder_and_create_only_missing(self):
        result=resolve_destination_paths(select_records(self.src,SECTION),self.states,{'Parent':'anchor'},SECTION)
        self.assertEqual(result['destination_paths'][SECTION],'root')
        self.assertEqual([r['logical_path'] for r in result['missing_folders']],[SECTION+'/Year'])
        self.assertEqual(result['path_provenance'][SECTION]['kind'],'EXPLICIT_SOURCE_FOLDER_REUSE')
        self.assertNotIn('created_folder_id',result['missing_folders'][0])

    def test_folder_logical_key_survives_separate_rename(self):
        self.states['child']=state('child','Old collection',['root'],True)
        self.src['records']['Rchild']['row']['Proposed name + type suffix']='Better Collection'
        self.src['records']['Mchild']=rec('Mchild','child','MOVE',SECTION+'/Old collection')
        result=resolve_destination_paths(select_records(self.src,SECTION),self.states,{'Parent':'anchor'},SECTION)
        self.assertEqual(result['destination_paths'][SECTION+'/Old collection'],'child')
        self.assertNotIn(SECTION+'/Better Collection',result['destination_paths'])

    def test_rename_only_folder_uses_referenced_child_alias(self):
        self.states['child']=state('child','Old collection',['root'],True)
        self.src['records']['Rchild']['row']['Proposed name + type suffix']='Better Collection'
        self.src['records']['Mother']['row']['Exact target path']=SECTION+'/Better Collection/Other.pdf'
        self.src['records']['Rother']['row']['Proposed destination folder']=SECTION+'/Better Collection'
        result=resolve_destination_paths(select_records(self.src,SECTION),self.states,{'Parent':'anchor'},SECTION)
        self.assertEqual(result['destination_paths'][SECTION+'/Better Collection'],'child')
        self.assertEqual(result['missing_folders'],[])
        self.src['records']['Mother']['row']['Exact target path']=SECTION+'/Old collection/Other.pdf'
        with self.assertRaisesRegex(SafetyError,'AMBIGUOUS_RENAMED_FOLDER'):
            resolve_destination_paths(select_records(self.src,SECTION),self.states,{'Parent':'anchor'},SECTION)

    def test_ambiguous_folder_path_rejected(self):
        self.states['duplicate']=state('duplicate','D',['old'],True)
        self.src['records']['Md']=rec('Md','duplicate','MOVE',SECTION)
        with self.assertRaisesRegex(SafetyError,'MULTIPLE_IDENTITIES'):
            resolve_destination_paths(select_records(self.src,SECTION),self.states,{'Parent':'anchor'},SECTION)

    def build(self):
        return build_preparation(self.src,section_path=SECTION,section_id='skills',workbook_id='fixture',metadata_by_id=self.states,
            destination_paths={'Parent':'anchor',SECTION:'root',SECTION+'/Year':'year'},destination_path_evidence_ref='fixture-proof')

    def test_nested_order_does_not_block_unrelated_group(self):
        result=self.build(); ops={o['reference']:o for o in result['projection']['operations']}
        self.assertEqual(ops['Mroot']['depends_on_refs'],['Rroot'])
        self.assertEqual(ops['Rchild']['depends_on_refs'],['Mroot'])
        self.assertEqual(ops['Rother']['depends_on_refs'],[])
        self.assertEqual(ops['Mother']['depends_on_refs'],['Rother'])
        child=next(m for m in result['projection']['members'] if m['source_id']=='child')
        self.assertEqual(child['ancestor_move_operation_refs'],['Mroot'])
        self.assertEqual(result,self.build())

    def test_unknown_partition_descendant_blocks(self):
        self.src['records']['Mroot']['row']['Scope']='Exact source-ID subtree partition; exclude separately scoped descendants'
        self.states['root']['subtree']['members'].append({'id':'unknown'})
        with self.assertRaisesRegex(SafetyError,'PARTITION_NOT_COMPLETE'):
            self.build()

    def test_whole_subtree_includes_unlisted_but_not_excluded(self):
        self.states['root']['subtree']['members'].append({'id':'known-unlisted'})
        result=self.build()
        self.assertIn('known-unlisted',{m['source_id'] for m in result['projection']['members']})
        self.src['excluded_ids']=['known-unlisted']
        with self.assertRaisesRegex(SafetyError,'EXCLUDED_FOLDER_DESCENDANT'):
            self.build()

    def test_native_title_requires_proof_and_preserves_upload_extension(self):
        self.states['child']['mime_type']='application/vnd.google-apps.document'
        self.src['records']['Rchild']['row']['Proposed name + type suffix']='New.gdoc'
        with self.assertRaisesRegex(SafetyError,'VERIFIED_NATIVE'):
            self.build()
        result=build_preparation(self.src,section_path=SECTION,section_id='skills',workbook_id='fixture',metadata_by_id=self.states,
            destination_paths={'Parent':'anchor',SECTION:'root',SECTION+'/Year':'year'},destination_path_evidence_ref='fixture-proof',
            native_conversion_refs={'Rchild':'native-proof'})
        self.assertEqual(result['resolutions']['Rchild']['exact_target_name'],'New')
        self.assertEqual(result['resolutions']['Rother']['exact_target_name'],'New Other.pdf')

    def test_target_inversion_has_child_first_and_no_cycle(self):
        self.states['child']=state('child','Child',['root'],True)
        self.states['root']['subtree']['members']=[{'id':'child','name':'Child','parents':['root'],'mime_type':FOLDER}]
        self.src=source(rec('Mroot','root','MOVE',SECTION+'/Child/Root'),rec('Mchild','child','MOVE',SECTION+'/Child'))
        result=build_preparation(self.src,section_path=SECTION,section_id='skills',workbook_id='fixture',metadata_by_id=self.states,
            destination_paths={'Parent':'anchor',SECTION:'section',SECTION+'/Child':'child'},destination_path_evidence_ref='proof')
        ops=result['projection']['operations']
        self.assertEqual([o['reference'] for o in ops],['Mchild','Mroot'])
        self.assertEqual(ops[1]['depends_on_refs'],['Mchild'])

if __name__=='__main__': unittest.main()
