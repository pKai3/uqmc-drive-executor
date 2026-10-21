import copy
import unittest
from test_importer import metadata,append_row
import test_nested_section_importer as nested_fixtures
from uqmc_recovery.agent_profile import agent_snapshot
from uqmc_recovery.importer import WORKBOOK_ID,HEADERS,catalog,import_workbook,destination_route_record
from uqmc_recovery.model import FOLDER,digest,SafetyError
from uqmc_recovery.section_importer import import_section,validate_section_authority
from uqmc_recovery.section_prepare import select_records,resolve_destination_paths,build_preparation


class DestinationScopedImporterTests(unittest.TestCase):
    def setUp(self):
        self.fixture=nested_fixtures.NestedSectionImporterTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        f=self.fixture
        f.states['year']['subtree']['members'].append({'id':'unrelated-eoi','name':'EOI Responses','mime_type':'application/vnd.google-apps.spreadsheet','parents':['year']})
        append_row(f.export,'Proposed Renames',{'Rename ID':'RENAME-OTHER','Source ID':'unrelated-eoi','Current name + type suffix':'EOI Responses.gsheet','Proposed name + type suffix':'2027 EOI.gsheet','Proposed destination folder':'Club/2027/Training'})
        f.states['new-year']=metadata('new-year','2024',FOLDER,['skills'])
        f.states['new-nested']=metadata('new-nested','Nested',FOLDER,['new-year'])
        self.states={sid:agent_snapshot({**state,'parents_complete':True}) for sid,state in f.states.items()}
        self.paths={'Club':'club','Club/Skills':'skills','Club/Skills/2024':'new-year','Club/Skills/2024/Nested':'new-nested'}

    def prepare(self):
        f=self.fixture;source=catalog(f.export)
        proof=f.journal.evidence({'kind':'FIXTURE_EXACT_METADATA'})
        pathproof=f.journal.evidence({'schema_version':'uqmc.section.destination-paths.v1','section_id':'skills','input_sha256':source['input_sha256'],'destination_paths':self.paths,'provenance_ref':proof})
        return build_preparation(source,section_path='Club/Skills',section_id='skills',workbook_id=WORKBOOK_ID,
            metadata_by_id=self.states,destination_paths=self.paths,destination_path_evidence_ref=pathproof,
            method_profile='destination_scoped_files')

    def run_import(self,prepared=None):
        f=self.fixture;prepared=prepared or self.prepare();p=prepared['projection']
        owner=f.journal.evidence({'schema_version':'uqmc.section.owner-approval.v1','decision':'APPROVED','actor':'fixture-user','approval_id':'dest-owner','approved_at_utc':'2026-01-01T00:00:00Z','provenance':{'kind':'explicit_owner_instruction','evidence_reference':'fixture-request'},'section_id':'skills','method_profile':p['method_profile'],'projection_revision':p['projection_revision'],'projection_sha256':digest(p),'member_source_ids':[m['source_id'] for m in p['members']],'operation_refs':prepared['selected_refs'],'allowed_actions':p['allowed_actions'],'allowed_destination_parent_ids':p['allowed_destination_parent_ids']})
        return import_section(f.journal,f.export,projection=p,owner_approval_ref=owner,expected_section_id='skills',batch_id='destination-batch',metadata_by_id=self.states,resolutions=prepared['resolutions'],expected_method_profile='destination_scoped_files',metadata_profile='AGENT_ASSISTED')

    def test_mixed_source_container_stays_while_exact_leaf_imports(self):
        result=self.run_import()
        self.assertIsNotNone(result.plan,result.manifest)
        self.assertEqual([(o.file_id,o.action) for o in result.plan.operations],[('file','RENAME'),('file','MOVE')])
        self.assertEqual(result.plan.operations[-1].target,{'parents':['new-nested']})
        self.assertEqual(result.plan.operations[-1].proposal_refs,('RENAME-003',))
        self.assertEqual(self.states['year']['parents'],['old'])
        self.assertEqual(validate_section_authority(self.fixture.journal,result.plan)['section_id'],'skills')
        self.assertEqual(self.fixture.export['sheets']['Proposed Renames'][3][HEADERS['Proposed Renames'].index('Decision (Yes/No)')],'')
        self.assertFalse(any(e['kind']=='ATTEMPTED' for e in self.fixture.journal.records()))

    def test_literal_slash_in_file_title_does_not_change_destination(self):
        self.fixture.export['sheets']['Proposed Renames'][3][HEADERS['Proposed Renames'].index('Proposed name + type suffix')]='New/Name.pdf'
        result=self.run_import();self.assertIsNotNone(result.plan,result.manifest)
        self.assertEqual(result.plan.operations[-1].target,{'parents':['new-nested']})
        self.assertEqual(result.plan.operations[0].target,{'name':'New/Name.pdf'})

    def test_source_container_cannot_be_destination(self):
        prepared=self.prepare();prepared['projection']['destination_paths']['Club/Skills/2024']='year'
        result=self.run_import(prepared)
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest['section_reason_code'],'SOURCE_CONTAINERS_MUST_REMAIN_IN_PLACE')

    def test_wrong_derived_target_rejected_even_consistent_resolution(self):
        prepared=self.prepare();move=prepared['projection']['operations'][-1]
        move['target_parent_ids']=['skills'];prepared['resolutions'][move['reference']]['target_parent_ids']=['skills'];prepared['projection']['allowed_destination_parent_ids']=['skills']
        result=self.run_import(prepared);self.assertIsNone(result.plan)
        self.assertIn(result.manifest['section_reason_code'],{'DESTINATION_ROUTE_TARGET_MISMATCH','DESTINATION_RENAME_PARENT_MISMATCH'})

    def test_route_cannot_bypass_plain_workbook_import(self):
        p=self.prepare();result=import_workbook(self.fixture.journal,self.fixture.export,batch_id='plain',selected_refs=['ROUTE/RENAME-003'],metadata_by_id=self.states,resolutions=p['resolutions'],metadata_profile='AGENT_ASSISTED')
        self.assertIsNone(result.plan);self.assertEqual(result.manifest['rows'][0]['reason_code'],'SELECTED_REFERENCE_MISSING')

    def test_derived_origin_hash_staleness_fails(self):
        prepared=self.prepare();prepared['resolutions']['ROUTE/RENAME-003']['row_sha256']='wrong'
        result=self.run_import(prepared);self.assertIsNone(result.plan)
        self.assertTrue(any(r['reason_code']=='STALE_TARGET_RESOLUTION' for r in result.manifest['rows']))

    def test_section_revocation_blocks_derived_plan(self):
        result=self.run_import();self.assertIsNotNone(result.plan,result.manifest)
        self.fixture.journal.append('SECTION_APPROVAL_REVOKED',payload={'approval_id':'dest-owner'})
        with self.assertRaisesRegex(SafetyError,'REVOKED_OR_SUPERSEDED'):validate_section_authority(self.fixture.journal,result.plan)

    def test_explicit_leaf_move_kept_without_duplicate_route(self):
        append_row(self.fixture.export,'Proposed Moves',{'Proposal ID':'MOVE-FILE','Action':'Move'})
        append_row(self.fixture.export,'Move Manifest',{'Proposal ID':'MOVE-FILE','Manifest item':'ITEM-FILE','Source ID':'file','Exact target path':'Club/Skills/2024/Nested/New.pdf'})
        prepared=self.prepare();self.assertEqual(set(prepared['selected_refs']),{'MOVE-FILE/ITEM-FILE','RENAME-003'})
        result=self.run_import(prepared);self.assertIsNotNone(result.plan,result.manifest)

if __name__=='__main__':unittest.main()
