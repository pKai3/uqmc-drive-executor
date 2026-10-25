"""Current proposal topology and explicitly reconciled reporting survive refresh."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import test_publication_projection as fixture
from test_viewer_publication import node
from uqmc_recovery import viewer_publication as viewer
from uqmc_recovery import destination_publication as publisher
from uqmc_recovery.publication_projection import build_publication


class PublisherPreservationTests(unittest.TestCase):
    def reporting_fixture(self):
        f=fixture.PublicationTests();f.setUp();f.applied()
        f.desired('STOPPED',state='CONFLICT',reason='PROTECTED_METADATA_DRIFT')
        before=f.publish();item=copy.deepcopy(f.item(before))
        item['prior_reporting_observation']=copy.deepcopy(next(i for i in before['html']['items'] if i['source_id']=='file'))
        at='2026-10-24T12:46:22Z'
        item['observed'].update(read_state='VERIFIED',metadata_verified_at_utc=at)
        item['location'].update(effect='DIRECT',verified_path='Root/Section/new.pdf',path_verified_at_utc=at)
        for field in ('move','rename'):
            item[field].update(effect='APPLIED',last_event_state='VERIFIED_APPLIED',verified_at_utc=at)
        item['reason_codes']=['LIVE_TARGET_MATCH_CONFIRMED']
        records=f.j.records()
        proof={'base_journal_checkpoint':len(records),'base_journal_tip_sha256':records[-1]['hash'],
               'verified_at_utc':at,'items':[item]}
        return f,proof,before

    def project(self,f,proof):
        return build_publication(f.j,allowed_source_ids=f.allowed,
            identity_basis={'identity_basis_version':1,'source_ids':f.allowed},reporting_corrections=proof)

    def test_live_reporting_correction_keeps_history_and_public_redaction(self):
        f,proof,before=self.reporting_fixture();history=f.j.records()
        after=self.project(f,proof);item=f.item(after)
        self.assertEqual(item['move']['effect'],'APPLIED');self.assertEqual(item['rename']['effect'],'APPLIED')
        self.assertEqual(item['observed']['read_state'],'VERIFIED')
        self.assertEqual(after['operation_log']['rows'],before['operation_log']['rows'])
        self.assertEqual(f.j.records(),history)
        public=next(i for i in after['html']['items'] if i['source_id']=='file')
        self.assertIsNone(public['observed']['exact_name']);self.assertIsNone(public['location']['verified_path'])
        self.assertNotIn('prior_reporting_observation',public)

    def test_later_source_activity_supersedes_old_reporting_correction(self):
        f,proof,_=self.reporting_fixture()
        f.desired('STOPPED',state='CONFLICT',reason='PARENT_CHANGED')
        self.assertEqual(f.item(self.project(f,proof))['move']['effect'],'CONFLICT')

    def test_real_name_or_parent_change_cannot_be_reconciled_by_old_proof(self):
        for field,value in [('exact_name','Different.pdf'),('parent_ids',['other-parent'])]:
            f,proof,_=self.reporting_fixture();proof['items'][0]['observed'][field]=value
            with self.assertRaisesRegex(ValueError,'TARGET_CHANGED'):self.project(f,proof)

    def test_wrong_journal_or_preimage_is_rejected(self):
        f,proof,_=self.reporting_fixture();proof['base_journal_tip_sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'JOURNAL_MISMATCH'):self.project(f,proof)
        f,proof,_=self.reporting_fixture();proof['items'][0]['prior_reporting_observation']['reason_codes']=['different']
        with self.assertRaisesRegex(ValueError,'PREIMAGE_MISMATCH'):self.project(f,proof)

    def test_private_receipt_survives_public_observation_redaction(self):
        f,proof,_=self.reporting_fixture()
        data={'live_reconciliation':{'base_journal_checkpoint':proof['base_journal_checkpoint'],
            'verified_at_utc':proof['verified_at_utc'],'resolved_move_rename_conflicts':['file']},
            'execution':{'journal_sequence':proof['base_journal_checkpoint'],
                'journal_tip_sha256':proof['base_journal_tip_sha256'],'items':proof['items']}}
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(publisher.retained_reporting_evidence(root,data),proof)
            publisher._save(Path(root)/'reporting-reconciliation.json',proof)
            data['execution']={'journal_sequence':999,'items':[]}
            self.assertEqual(publisher.retained_reporting_evidence(root,data),proof)
            data['live_reconciliation']['resolved_move_rename_conflicts'].append('unapproved')
            with self.assertRaisesRegex(ValueError,'RECEIPT_CHANGED'):
                publisher.retained_reporting_evidence(root,data)

    def test_appended_year_parent_is_resolved_without_array_order_assumption(self):
        data={'nodes':[node(0,-1,'Root'),node(1,2,'file.pdf',False,'file'),node(2,0,'2012')],
              'roots':{'destination':[0],'retained':[]},'notes':[]}
        workbook={'exported_at_utc':'2026-10-25T00:00:00Z','sheets':{'Proposed Renames':[
            ['Source ID','Proposed name + type suffix','Proposed destination folder'],
            ['file','file.pdf','Root/2012']]}}
        self.assertEqual(viewer.node_paths(data)[1],('Root','2012','file.pdf'))
        self.assertEqual(viewer.proposed_folder_updates(data,workbook),[])
        viewer.refresh_proposal(data,workbook)
        self.assertEqual([n['n'] for n in data['nodes']],['Root','2012','file.pdf'])
        self.assertEqual(data['nodes'][2]['id'],'file')

    def test_missing_parent_and_cycle_fail_with_explicit_errors(self):
        with self.assertRaisesRegex(ValueError,'PARENT_MISSING'):
            viewer.node_paths({'nodes':[node(0,42,'bad')]})
        with self.assertRaisesRegex(ValueError,'TREE_CYCLE'):
            viewer.node_paths({'nodes':[node(0,1,'A'),node(1,0,'B')]})

    def test_revision_annotation_and_natural_sort_script_are_preserved(self):
        data={'nodes':[node(0,-1,'Root'),node(1,0,'a.pdf',False,'file')],
              'execution_sections':{'sections':[]},'proposal_revision_summary':{'governance_cleanup':{'approved':'notes'}},
              'execution':{'journal_sequence':5}}
        viewer.mark_proposed_revision_status(data,[])
        self.assertEqual(data['proposal_revision_summary']['governance_cleanup'],{'approved':'notes'})
        sorting="const siblingCollator=new Intl.Collator('en-AU',{numeric:true,sensitivity:'base'});"
        source='<html><style></style><script id="tree-data"></script><script>'+sorting+'</script></html>'
        self.assertIn(sorting,viewer.render(source,data))


if __name__=='__main__':unittest.main()
