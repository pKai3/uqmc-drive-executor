import copy
import unittest
from test_projection import MemoryJournal
from uqmc_recovery.destination_publication import revision_folder_updates
from uqmc_recovery import viewer_publication as viewer
from test_viewer_revision import tree, workbook

FOLDER='application/vnd.google-apps.folder'

class ManualFolderReportingTests(unittest.TestCase):
    def make(self, change=None):
        j=MemoryJournal()
        before={'id':'folder','name':'2024','parents':['general'],'mimeType':FOLDER,'trashed':False}
        observed={**before,'parents':['new-parent']}
        parent={'id':'new-parent','name':'New','parents':['root'],'mimeType':FOLDER,'trashed':False}
        proof={'schema_version':'uqmc.manual-folder-relocation.v1','actor':'user','source_id':'folder',
            'instruction':'I moved this folder manually; preserve that move.',
            'before_path':'Root/General/2024','target_path':'Root/New/2024','parent_path':'Root/New',
            'prior_observation_ref':j.evidence(before),'parent_observed_ref':j.evidence(parent)}
        if change: change(proof, observed)
        j.append('DESTINATION_VERIFIED',payload={'file_id':'folder','section_id':'section','exact_name':'2024',
            'parent_ids':['new-parent'],'proposal_path':'Root/New/2024','observed_ref':j.evidence(observed),
            'manual_relocation_ref':j.evidence(proof)})
        return j

    def test_human_move_remains_observation_and_reparents_same_viewer_identity(self):
        j=self.make(); revisions=revision_folder_updates(j)
        self.assertEqual(revisions[0]['performed_by'],'user')
        self.assertEqual([e['kind'] for e in j.records()],['DESTINATION_VERIFIED'])
        data=tree(); w=workbook()
        w['sheets']['Proposed Renames']=[w['sheets']['Proposed Renames'][0],['file','File.pdf','Root/New/2024']]
        viewer.refresh_proposal(data,w,folder_revisions=revisions)
        node=viewer.resolve_folder_path(data,'Root/New/2024')
        self.assertEqual(node['id'],'folder'); self.assertNotIn('pending_revision',node)
        self.assertEqual(next(n for n in data['nodes'] if n['id']=='file')['p'],node['i'])

    def test_mismatched_identity_parent_or_actor_is_rejected(self):
        changes=[lambda p,o:o.update(id='wrong'), lambda p,o:o.update(parents=['wrong']),
                 lambda p,o:p.update(actor='assistant'), lambda p,o:o.update(trashed=True),
                 lambda p,o:p.update(target_path='Root/Other/2024')]
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError,'MANUAL_FOLDER_RELOCATION_EVIDENCE_MISMATCH'):
                revision_folder_updates(self.make(change))

    def test_ordinary_verification_does_not_invent_a_relocation(self):
        payload=self.make().records()[0]['payload']; payload.pop('manual_relocation_ref')
        j=MemoryJournal(); j.append('DESTINATION_VERIFIED',payload=payload)
        self.assertEqual(revision_folder_updates(j),[])

    def test_inherited_logical_path_preserves_original_observation(self):
        from uqmc_recovery.publication_projection import _carried_folder_observations
        old={'id':'child','name':'Child','parent_ids':['folder'],'path':'Root/General/2024/Child','verified_at_utc':'old-time'}
        other={**old,'id':'other','path':'Root/General/20240/Child'}
        result=_carried_folder_observations(self.make(),{('section','child'):old,('section','other'):other})
        self.assertEqual(result[('section','child')],{**old,'path':'Root/New/2024/Child'})
        self.assertEqual(result[('section','other')],other)
