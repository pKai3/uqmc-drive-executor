"""Offline trust-boundary checks for deterministic destination hybrid imports."""
import copy
import unittest

import test_nested_section_importer as fixtures
from test_importer import append_row, metadata
from uqmc_recovery.agent_profile import agent_snapshot
from uqmc_recovery.destination_planner import plan_destination, build_hybrid_preparation
from uqmc_recovery.importer import catalog, WORKBOOK_ID, HEADERS, import_workbook
from uqmc_recovery.model import FOLDER, SafetyError, digest
from uqmc_recovery.section_importer import import_section, validate_section_authority


class HybridImporterTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.NestedSectionImporterTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.states={sid:agent_snapshot({**value,'parents_complete':True}) for sid,value in self.fixture.states.items()}
        self.paths={'Club':'club','Club/Skills':'skills'}

    def prepare(self):
        f=self.fixture;source=catalog(f.export)
        layout=plan_destination(source,section_path='Club/Skills',metadata_by_id=self.states,anchor_paths=self.paths)
        paths=layout['destination_paths']
        proof=f.journal.evidence({'kind':'EXACT_FIXTURE_METADATA'})
        pathproof=f.journal.evidence({'schema_version':'uqmc.section.destination-paths.v1','section_id':'skills',
            'input_sha256':source['input_sha256'],'destination_paths':paths,'provenance_ref':proof})
        return build_hybrid_preparation(source,section_path='Club/Skills',section_id='skills',workbook_id=WORKBOOK_ID,
            metadata_by_id=self.states,destination_paths=paths,destination_path_evidence_ref=pathproof)

    def run_import(self,prepared=None):
        prepared=prepared or self.prepare();p=prepared['projection'];f=self.fixture
        owner=f.journal.evidence({'schema_version':'uqmc.section.owner-approval.v1','decision':'APPROVED','actor':'fixture-user',
            'approval_id':'hybrid-owner','approved_at_utc':'2026-01-01T00:00:00Z',
            'provenance':{'kind':'explicit_owner_instruction','evidence_reference':'fixture-request'},
            'section_id':'skills','method_profile':p['method_profile'],'projection_revision':p['projection_revision'],
            'projection_sha256':digest(p),'member_source_ids':[m['source_id'] for m in p['members']],
            'operation_refs':prepared['selected_refs'],'allowed_actions':p['allowed_actions'],
            'allowed_destination_parent_ids':p['allowed_destination_parent_ids']})
        return import_section(f.journal,f.export,projection=p,owner_approval_ref=owner,expected_section_id='skills',
            batch_id='hybrid-batch',metadata_by_id=self.states,resolutions=prepared['resolutions'],
            expected_method_profile='destination_hybrid',metadata_profile='AGENT_ASSISTED')

    def test_intact_nested_source_imports_with_derived_routes(self):
        result=self.run_import()
        self.assertIsNotNone(result.plan,result.manifest)
        self.assertEqual(len(result.plan.operations),6)
        self.assertEqual({o.file_id for o in result.plan.operations},{'year','nested','file'})
        self.assertEqual(validate_section_authority(self.fixture.journal,result.plan)['section_id'],'skills')
        self.assertFalse(any(e['kind']=='ATTEMPTED' for e in self.fixture.journal.records()))

    def test_mixed_source_kept_but_intact_nested_source_reused(self):
        self.states['year']['subtree']['members'].append({'id':'outside','name':'EOI','mime_type':'application/pdf','parents':['year']})
        self.states['new-year']=agent_snapshot({**metadata('new-year','2024',FOLDER,['skills']),'parents_complete':True})
        self.paths['Club/Skills/2024']='new-year'
        result=self.run_import()
        self.assertIsNotNone(result.plan,result.manifest)
        self.assertEqual({o.file_id for o in result.plan.operations},{'nested','file'})
        self.assertEqual(next(o for o in result.plan.operations if o.file_id=='nested' and o.action=='MOVE').target,{'parents':['new-year']})

    def test_consistently_wrong_name_in_projection_and_resolution_rejected(self):
        prepared=self.prepare();op=next(o for o in prepared['projection']['operations'] if o['source_id']=='file' and o['action']=='RENAME')
        op['exact_target_name']='Wrong.pdf';prepared['resolutions'][op['reference']]['exact_target_name']='Wrong.pdf'
        result=self.run_import(prepared)
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest['section_reason_code'],'HYBRID_DETERMINISTIC_PROJECTION_MISMATCH')

    def test_unrelated_destination_identity_rejected(self):
        self.states['skills']['parents']=['old']
        result=self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest['section_reason_code'],'HYBRID_DESTINATION_FOLDER_PLACEMENT_MISMATCH')

    def test_route_cannot_bypass_plain_import(self):
        prepared=self.prepare()
        result=import_workbook(self.fixture.journal,self.fixture.export,batch_id='plain',selected_refs=['ROUTE/RENAME-003'],
            metadata_by_id=self.states,resolutions=prepared['resolutions'],metadata_profile='AGENT_ASSISTED')
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest['rows'][0]['reason_code'],'SELECTED_REFERENCE_MISSING')

    def test_stale_origin_row_hash_rejected(self):
        prepared=self.prepare();prepared['resolutions']['ROUTE/RENAME-003']['row_sha256']='stale'
        result=self.run_import(prepared)
        self.assertIsNone(result.plan)
        self.assertTrue(any(r['reason_code']=='STALE_TARGET_RESOLUTION' for r in result.manifest['rows']))

    def test_late_authority_revocation_still_blocks(self):
        result=self.run_import();self.assertIsNotNone(result.plan,result.manifest)
        self.fixture.journal.append('SECTION_APPROVAL_REVOKED',payload={'approval_id':'hybrid-owner'})
        with self.assertRaisesRegex(SafetyError,'REVOKED_OR_SUPERSEDED'):
            validate_section_authority(self.fixture.journal,result.plan)

    def test_top_level_section_binds_explicit_root_identity(self):
        f=self.fixture
        for rows in f.export['sheets'].values():
            for row in rows[1:]:
                for index,value in enumerate(row):
                    if isinstance(value,str) and value.startswith('Club/'):
                        row[index]=value[len('Club/'):]
        source=catalog(f.export)
        paths={'':'club','Skills':'skills'}
        self.states['club']['name']='Actual project root title'
        layout=plan_destination(source,section_path='Skills',metadata_by_id=self.states,anchor_paths=paths)
        proof=f.journal.evidence({'kind':'EXACT_FIXTURE_METADATA'})
        pathproof=f.journal.evidence({'schema_version':'uqmc.section.destination-paths.v1','section_id':'skills',
            'input_sha256':source['input_sha256'],'destination_paths':layout['destination_paths'],'provenance_ref':proof})
        prepared=build_hybrid_preparation(source,section_path='Skills',section_id='skills',workbook_id=WORKBOOK_ID,
            metadata_by_id=self.states,destination_paths=layout['destination_paths'],destination_path_evidence_ref=pathproof)
        result=self.run_import(prepared)
        self.assertIsNotNone(result.plan,result.manifest)
        self.assertNotIn('club',{o.file_id for o in result.plan.operations})

    def test_current_satisfied_fields_do_not_corrupt_original_row_binding(self):
        for sid,name in [('year','2024'),('nested','Nested'),('file','New.pdf')]:
            self.states[sid]['name']=name
        self.states['year']['parents']=['skills']
        for sid in ('year','nested'):
            for member in self.states[sid]['subtree']['members']:
                member['name']=self.states[member['id']]['name']
        result=self.run_import();self.assertIsNotNone(result.plan,result.manifest)
        self.assertTrue(all(o.before['name']==o.intended()['name'] for o in result.plan.operations))
        self.assertTrue(all(o.before['parents']==o.intended()['parents'] for o in result.plan.operations))


    def test_durable_exclusion_rebuilds_same_filtered_projection(self):
        f=self.fixture
        f.journal.append('PROPOSAL_EXCLUDED',payload={'proposal_ref':'RENAME-003'})
        append_row(f.export,'Proposed Renames',{'Rename ID':'R-other','Source ID':'other',
            'Current name + type suffix':'Other.pdf','Proposed name + type suffix':'Other approved.pdf',
            'Proposed destination folder':'Club/Skills'})
        self.states['other']=agent_snapshot({**metadata('other','Other.pdf','application/pdf',['old']),'parents_complete':True})
        for sid,name,parent,path in [('new-year','2024','skills','Club/Skills/2024'),
                                     ('new-nested','Nested','new-year','Club/Skills/2024/Nested')]:
            self.states[sid]=agent_snapshot({**metadata(sid,name,FOLDER,[parent]),'parents_complete':True})
            self.paths[path]=sid
        source=catalog(f.export);source['excluded_refs'].append('RENAME-003')
        proof=f.journal.evidence({'kind':'EXACT_FIXTURE_METADATA'})
        pathproof=f.journal.evidence({'schema_version':'uqmc.section.destination-paths.v1','section_id':'skills',
            'input_sha256':source['input_sha256'],'destination_paths':self.paths,'provenance_ref':proof})
        prepared=build_hybrid_preparation(source,section_path='Club/Skills',section_id='skills',workbook_id=WORKBOOK_ID,
            metadata_by_id=self.states,destination_paths=self.paths,destination_path_evidence_ref=pathproof)
        result=self.run_import(prepared)
        self.assertIsNotNone(result.plan,result.manifest)
        self.assertEqual({o.file_id for o in result.plan.operations},{'other'})


if __name__=='__main__':unittest.main()
