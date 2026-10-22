"""Creation lineage grants coverage only after exact verified allocation."""
import copy
import tempfile
import unittest
from uqmc_recovery.destination_recovery import load_created_folder_proofs, load_created_folder_proofs_for_path
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, SafetyError, digest


class CreatedFolderProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.j=Journal(self.tmp.name);self.section='fixture-section'
        self.provenance=self.j.evidence({'kind':'TRUSTED_USER_INSTRUCTION','text':'Complete fixture destination'})

    def authority(self, folders, **overrides):
        return self.j.evidence({'schema_version':'uqmc.folder-create-authority.v1',
            'section_id':self.section,'approval_id':'fixture-approval','decision':'APPROVED',
            'access_policy':'SCOPED_INHERITANCE_ACCEPTED','provenance_ref':self.provenance,
            'folders':folders,**overrides})

    def create(self, sid='folder', key='key:folder', name='Folder', parent='anchor', *, ref=None,
               verify=True, observed_changes=None, section=None, allocate=True):
        section=section or self.section
        ref=ref or self.authority([{'key':key,'name':name,'parent_id':parent}])
        stable=digest({'section_id':section,'key':key});op='required-folder-'+stable[:24]
        common={'section_id':section,'file_id':sid,'key':key,'name':name,'parent_id':parent,'authority_ref':ref}
        if allocate:self.j.append('REQUIRED_FOLDER_ALLOCATED',op,payload={**common,'stable_key':stable})
        if verify:
            observed={'id':sid,'name':name,'mimeType':FOLDER,'parents':[parent],'trashed':False,
                'appProperties':{'uqmcFolderKey':stable},**(observed_changes or {})}
            self.j.append('REQUIRED_FOLDER_VERIFIED',op,payload={**common,'observed_ref':self.j.evidence(observed),'reconciled':True})
        return op,common

    def test_verified_reconciliation_proof_retains_exact_lineage(self):
        op,common=self.create()
        proof=load_created_folder_proofs(self.j,self.section)['folder']
        self.assertEqual(proof['logical_key'],'key:folder')
        self.assertEqual(proof['parent_id'],'anchor')
        self.assertEqual(proof['allocation_event_ref']['sequence'],1)
        self.assertEqual(proof['verification_event_ref']['sequence'],2)
        self.assertEqual(len(self.j.records()),2)
        self.create(allocate=False,ref=common['authority_ref'])
        self.assertEqual(load_created_folder_proofs(self.j,self.section)['folder']['verification_event_ref']['sequence'],3)

    def test_unverified_or_uncertain_only_has_no_coverage(self):
        op,common=self.create(verify=False)
        self.j.append('REQUIRED_FOLDER_UNCERTAIN',op,payload=common)
        self.assertEqual(load_created_folder_proofs(self.j,self.section),{})

    def test_later_uncertain_request_requires_reconciliation_before_coverage(self):
        op,common=self.create()
        self.j.append('REQUIRED_FOLDER_ATTEMPTED',op,payload=common)
        self.j.append('REQUIRED_FOLDER_UNCERTAIN',op,payload=common)
        with self.assertRaisesRegex(SafetyError,'UNRESOLVED_CREATION'):
            load_created_folder_proofs(self.j,self.section)
        self.create(allocate=False,ref=common['authority_ref'])
        self.assertIn('folder',load_created_folder_proofs(self.j,self.section))

    def test_other_section_is_not_eligible(self):
        self.create()
        self.assertEqual(load_created_folder_proofs(self.j,'other-section'),{})

    def test_verification_without_allocation_rejected(self):
        self.create(allocate=False)
        with self.assertRaisesRegex(SafetyError,'VERIFICATION_WITHOUT_ALLOCATION'):
            load_created_folder_proofs(self.j,self.section)

    def test_exact_observation_identity_name_parent_type_and_marker_required(self):
        changes=[{'id':'other'},{'name':'Wrong'},{'parents':['other']},
                 {'mimeType':'application/pdf'},{'trashed':True},{'appProperties':{}}]
        for change in changes:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                original=self.j;self.j=Journal(tmp)
                self.provenance=self.j.evidence({'kind':'TRUSTED_USER_INSTRUCTION','text':'Fixture'})
                self.create(observed_changes=change)
                with self.assertRaisesRegex(SafetyError,'OBSERVATION_MISMATCH'):
                    load_created_folder_proofs(self.j,self.section)
                self.j=original

    def test_duplicate_allocation_and_same_id_under_different_key_rejected(self):
        op,common=self.create()
        self.j.append('REQUIRED_FOLDER_ALLOCATED',op,payload={**common,'stable_key':digest({'section_id':self.section,'key':common['key']})})
        with self.assertRaisesRegex(SafetyError,'DUPLICATE_ALLOCATION'):
            load_created_folder_proofs(self.j,self.section)

    def test_same_id_under_two_keys_rejected(self):
        self.create();self.create(key='different-key')
        with self.assertRaisesRegex(SafetyError,'IDENTITY_CONFLICT'):
            load_created_folder_proofs(self.j,self.section)

    def test_revoked_or_superseded_authority_rejected(self):
        self.create();self.j.append('SECTION_APPROVAL_SUPERSEDED',payload={'approval_id':'fixture-approval'})
        with self.assertRaisesRegex(SafetyError,'AUTHORITY_REVOKED'):
            load_created_folder_proofs(self.j,self.section)

    def test_authority_section_or_parent_cannot_be_changed(self):
        ref=self.authority([{'key':'key:folder','name':'Folder','parent_id':'wrong-parent'}])
        self.create(ref=ref)
        with self.assertRaisesRegex(SafetyError,'PARENT_SCOPE_MISMATCH'):
            load_created_folder_proofs(self.j,self.section)

    def test_different_authority_scope_rejected(self):
        ref=self.authority([{'key':'key:folder','name':'Folder','parent_id':'anchor'}],section_id='other')
        self.create(ref=ref)
        with self.assertRaisesRegex(SafetyError,'AUTHORITY_SCOPE_MISMATCH'):
            load_created_folder_proofs(self.j,self.section)

    def test_parent_key_requires_prior_verified_same_section_identity(self):
        folders=[{'key':'key:parent','name':'Parent','parent_id':'anchor'},
                 {'key':'key:child','name':'Child','parent_key':'key:parent'}]
        ref=self.authority(folders)
        self.create(sid='parent',key='key:parent',name='Parent',ref=ref)
        self.create(sid='child',key='key:child',name='Child',parent='parent',ref=ref)
        proofs=load_created_folder_proofs(self.j,self.section)
        self.assertEqual(set(proofs),{'parent','child'})
        self.assertEqual(proofs['child']['parent_id'],'parent')

    def test_missing_or_later_verified_parent_rejected(self):
        ref=self.authority([{'key':'key:parent','name':'Parent','parent_id':'anchor'},
                            {'key':'key:child','name':'Child','parent_key':'key:parent'}])
        self.create(sid='child',key='key:child',name='Child',parent='parent',ref=ref)
        self.create(sid='parent',key='key:parent',name='Parent',ref=ref)
        with self.assertRaisesRegex(SafetyError,'PARENT_DEPENDENCY_UNVERIFIED'):
            load_created_folder_proofs(self.j,self.section)


class CreatedFolderPathProofTests(unittest.TestCase):
    setUp=CreatedFolderProofTests.setUp
    create=CreatedFolderProofTests.create
    authority=CreatedFolderProofTests.authority
    def at(self, creation_section, logical_path, sid, *, record_path=True, key=None):
        previous=self.section
        self.section='destination-'+digest({'canonical_path':creation_section})[:20]
        try:
            op,common=self.create(sid=sid,key=key or 'folder:'+digest({'section_path':creation_section,'logical_path':logical_path}),
                name=logical_path.rsplit('/',1)[-1])
            if record_path:self.destination_event(common,logical_path)
            return common
        finally:self.section=previous

    def destination_event(self, common, path, **changes):
        payload={'section_id':common['section_id'],'file_id':common['file_id'],
                 'exact_name':common['name'],'parent_ids':[common['parent_id']],
                 'proposal_path':path,**changes}
        self.j.append('DESTINATION_VERIFIED',payload=payload)

    def test_exact_scope_keeps_unbound_verified_creations_and_adds_origin(self):
        self.at('Club/Digital/Sub','Club/Digital/Sub/Year','exact',record_path=False)
        proof=load_created_folder_proofs_for_path(self.j,'Club/Digital/Sub')['exact']
        self.assertEqual(proof['creation_section_path'],'Club/Digital/Sub')
        self.assertNotIn('logical_path',proof)

    def test_ancestor_children_eligible_but_siblings_and_unrecorded_paths_excluded(self):
        self.at('Club/Digital','Club/Digital/Sub/Year','child')
        self.at('Club/Digital','Club/Digital/Other/Year','sibling')
        self.at('Club/Digital','Club/Digital/Sub/Unrecorded','unrecorded',record_path=False)
        result=load_created_folder_proofs_for_path(self.j,'Club/Digital/Sub')
        self.assertEqual(set(result),{'child'})
        self.assertEqual(result['child']['logical_path'],'Club/Digital/Sub/Year')
        self.assertEqual(result['child']['creation_section_path'],'Club/Digital')

    def test_unrelated_or_descendant_creation_scopes_do_not_widen_selection(self):
        self.at('Other','Other/Sub/Year','other')
        self.at('Club/Digital/Sub/Deep','Club/Digital/Sub/Deep/Year','deeper')
        self.assertEqual(load_created_folder_proofs_for_path(self.j,'Club/Digital/Sub'),{})

    def test_wrong_logical_key_for_claimed_path_rejected(self):
        self.at('Club/Digital','Club/Digital/Sub/Year','wrong',key='folder:wrong')
        with self.assertRaisesRegex(SafetyError,'LOGICAL_KEY_MISMATCH'):
            load_created_folder_proofs_for_path(self.j,'Club/Digital/Sub')

    def test_wrong_destination_name_or_parent_rejected(self):
        common=self.at('Club/Digital','Club/Digital/Sub/Year','child',record_path=False)
        self.destination_event(common,'Club/Digital/Sub/Year',parent_ids=['wrong'])
        with self.assertRaisesRegex(SafetyError,'DESTINATION_PATH_EVIDENCE_MISMATCH'):
            load_created_folder_proofs_for_path(self.j,'Club/Digital/Sub')

    def test_same_id_multiple_paths_is_not_silently_selected(self):
        common=self.at('Club/Digital','Club/Digital/Sub/Year','child')
        self.destination_event(common,'Club/Digital/Other/Year')
        with self.assertRaisesRegex(SafetyError,'LOGICAL_PATH_CONFLICT'):
            load_created_folder_proofs_for_path(self.j,'Club/Digital/Sub')

    def test_distinct_ancestor_ids_for_same_path_rejected(self):
        self.at('Club','Club/Digital/Sub/Year','first')
        self.at('Club/Digital','Club/Digital/Sub/Year','second')
        with self.assertRaisesRegex(SafetyError,'IDENTITY_CONFLICT'):
            load_created_folder_proofs_for_path(self.j,'Club/Digital/Sub')

    def test_exact_section_allocation_cannot_rebind_ancestor_path(self):
        self.at('Club/Digital','Club/Digital/Sub/Year','ancestor')
        self.at('Club/Digital/Sub','Club/Digital/Sub/Year','exact',record_path=False)
        with self.assertRaisesRegex(SafetyError,'IDENTITY_CONFLICT'):
            load_created_folder_proofs_for_path(self.j,'Club/Digital/Sub')


if __name__=='__main__':unittest.main()
