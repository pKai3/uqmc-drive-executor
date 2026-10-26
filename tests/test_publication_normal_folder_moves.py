"""Normal migration readbacks must settle proposed folder display revisions."""
import copy
import unittest
from test_projection import MemoryJournal
from test_viewer_revision import tree, workbook
from uqmc_recovery import destination_publication as p
from uqmc_recovery import viewer_publication as v


class NormalFolderMoveTests(unittest.TestCase):
    def setUp(self):
        self.j = MemoryJournal()
        self.before = {'id':'folder','name':'2024','parents':['general'],
                       'mime_type':'application/vnd.google-apps.folder','trashed':False}
        self.after = {**self.before,'name':'Minutes','parents':['year']}
        self.plan = {'operations':[
            {'operation_id':'rename','file_id':'folder','action':'RENAME',
             'before':self.before,'target':{'name':'Minutes'},'depends_on':[]},
            {'operation_id':'move','file_id':'folder','action':'MOVE',
             'before':{**self.before,'name':'Minutes'},'target':{'parents':['year']},'depends_on':['rename']}]}
        self.ph = p._sha(p._bytes(self.plan))
        self.j.append('DRAFTED',payload={'proposal_hash':self.ph,'plan_ref':self.j.evidence(self.plan)})
        self.j.append('APPROVED',payload={'proposal_hash':self.ph})
        self.observe()
        self.spec = {'section_id':'section','title':'Root','proposal_path':'Root',
            'source_ids':['file'],'destination_id':'root','destination_mode':'REUSED_EXISTING',
            'plan_sha256s':[self.ph], 'destination_paths':{
                'root':'Root','year':'Root/2024','folder':'Root/2024/Minutes'}}
        self.registry = {'sections':[{'publication_spec':self.spec}]}

    def observe(self, kind='DESIRED_APPLIED', raw=None, **extra):
        payload = {'source_id':'folder','plan_sha256':self.ph,'direction':'FORWARD',
                   'observed_ref':self.j.evidence(self.after if raw is None else raw),**extra}
        self.j.append(kind,payload=payload)

    def updates(self):
        return v.proposed_folder_updates(tree(),workbook())

    def reconcile(self):
        return p.reconcile_applied_folder_updates(self.j,self.registry,self.updates())

    def test_ordinary_approved_batch_proves_exact_folder_revision(self):
        before = copy.deepcopy(self.j.events)
        result = self.reconcile()
        self.assertTrue(result[0]['verified'])
        self.assertEqual(before,self.j.events)
        data=tree();v.refresh_proposal(data,workbook(),folder_revisions=result)
        folder=v.resolve_folder_path(data,'Root/2024/Minutes')
        self.assertEqual(folder['id'],'folder')
        self.assertEqual(next(n for n in data['nodes'] if n.get('id')=='file')['p'],folder['i'])
        self.assertNotIn('pending_revision',folder)

    def test_same_exact_proof_handles_cross_section_folders_and_all_forty_files(self):
        data=tree();data['nodes'][0]['n']='Old'
        data['nodes'].append(dict(data['nodes'][0],i=4,n='New',id='root'))
        for i in range(1,40):
            data['nodes'].append(dict(data['nodes'][3],i=len(data['nodes']),id='file'+str(i)))
        w=workbook();w['sheets']['Proposed Renames'][1][2]='New/2024/Minutes'
        w['sheets']['Proposed Renames'][2][2]='New/2024'
        for i in range(1,40):
            w['sheets']['Proposed Renames'].append(['file'+str(i),'File.pdf','New/2024/Minutes'])
        self.spec['proposal_path']='New'
        self.spec['destination_paths']={k:path.replace('Root','New') for k,path in self.spec['destination_paths'].items()}
        self.spec['source_ids']=['file']+['file'+str(i) for i in range(1,40)]
        updates=v.proposed_folder_updates(data,w)
        self.assertFalse(updates[0]['verified'])
        updates=p.reconcile_applied_folder_updates(self.j,self.registry,updates)
        v.refresh_proposal(data,w,folder_revisions=updates)
        specs=p.display_section_specs(self.registry,data,self.spec['source_ids'])
        self.assertEqual(len(specs[0]['source_ids']),40)
        self.assertTrue(updates[0]['verified'])

    def test_missing_approval_is_still_pending(self):
        self.j.events=[e for e in self.j.events if e['kind']!='APPROVED']
        self.assertFalse(self.reconcile()[0]['verified'])

    def test_fresh_plan_different_target_or_unregistered_plan_stays_pending(self):
        for change in ('path','hash'):
            with self.subTest(change=change):
                original=copy.deepcopy(self.spec)
                if change=='path':self.spec['destination_paths']['folder']='Root/Elsewhere'
                else:self.spec['plan_sha256s']=['different']
                self.assertFalse(self.reconcile()[0]['verified'])
                self.spec.clear();self.spec.update(original)

    def test_wrong_identity_parent_name_type_or_trashed_is_not_verified(self):
        for fields in ({'id':'other'},{'parents':['other']},{'name':'Other'},
                       {'mime_type':'text/plain'},{'trashed':True}):
            self.observe(raw={**self.after,**fields})
            self.assertFalse(self.reconcile()[0]['verified'],fields)

    def test_latest_uncertain_conflicting_or_rollback_event_wins(self):
        for kind,fields in [('DESIRED_STOPPED',{'state':'UNCERTAIN'}),
                            ('DESIRED_STOPPED',{'state':'CONFLICT'}),
                            ('DESIRED_PREPARED',{}),
                            ('DESIRED_ROLLED_BACK',{'direction':'ROLLBACK'})]:
            self.observe(kind,**fields)
            self.assertFalse(self.reconcile()[0]['verified'],kind)

    def test_reconciled_target_observation_can_verify_without_claiming_a_new_write(self):
        self.observe('DESIRED_OBSERVED',state='TARGET')
        self.assertTrue(self.reconcile()[0]['verified'])

    def test_later_destination_readback_drift_overrides_older_success(self):
        self.j.append('DESTINATION_VERIFIED',payload={
            'file_id':'folder','observed_ref':self.j.evidence({**self.after,'parents':['elsewhere']})})
        self.assertFalse(self.reconcile()[0]['verified'])

    def test_ambiguous_parent_ids_remain_a_conflict(self):
        self.spec['destination_paths']['other-year']='Root/2024'
        with self.assertRaisesRegex(ValueError,'VIEWER_REVISION_PARENT_PATH_AMBIGUOUS'):
            self.reconcile()

    def test_drafted_hash_mismatch_is_rejected(self):
        self.j.events[0]['payload']['plan_ref']=self.j.evidence({'operations':[]})
        with self.assertRaisesRegex(ValueError,'VIEWER_REVISION_PLAN_HASH_MISMATCH'):
            self.reconcile()

    def test_existing_explicit_revision_verification_is_preserved(self):
        updates=self.updates();updates[0]['verified']=True
        self.observe('DESIRED_STOPPED',state='UNCERTAIN')
        self.assertEqual(p.reconcile_applied_folder_updates(self.j,self.registry,updates),updates)


if __name__=='__main__':unittest.main()
