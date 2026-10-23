"""Policy scope and verified folder retirement are distinct from action history."""
import copy
import unittest
import test_retired_publication as retired_fixture
from uqmc_recovery import viewer_publication as viewer
from uqmc_recovery.sheet_publication import item_status
from uqmc_recovery.shortcut_policy import SHORTCUT_MIME, REASON


def exclude(journal, sid):
    journal.append('PLAN_EXCLUDED', payload={'file_id':sid,
        'mime_type':SHORTCUT_MIME,'reason_code':REASON})


class PolicyPublicationTests(unittest.TestCase):
    def test_applied_and_untracked_exclusions_preserve_historical_rows(self):
        f=retired_fixture.RetiredProjectionTests(methodName='runTest');f.setUp()
        before=f.f.publish(sections=[f.spec])
        for sid in ('file','untouched'):exclude(f.f.j,sid)
        after=f.f.publish(sections=[f.spec])
        s=after['sections']['sections'][0]
        self.assertEqual((s['expected_file_count'],s['completed_file_count'],s['policy_excluded_file_count']),(0,0,2))
        for sid in ('file','untouched'):
            old=f.f.item(before,sid);new=f.f.item(after,sid)
            for key in ('move','rename','observed'):self.assertEqual(new[key],old[key])
            self.assertIn('POLICY_EXCLUDED',new['reason_codes'])
            self.assertNotIn('RETIRED',new['reason_codes'])
        self.assertEqual(before['operation_log']['rows'],after['operation_log']['rows'])

    def test_conflict_remains_conflict_under_policy_label(self):
        f=retired_fixture.RetiredProjectionTests(methodName='runTest');f.setUp()
        f.f.desired('STOPPED',state='CONFLICT',reason_code='PROTECTED_METADATA_DRIFT')
        before=f.f.publish();exclude(f.f.j,'file');after=f.f.publish()
        self.assertEqual(f.f.item(before)['move'],f.f.item(after)['move'])
        self.assertEqual(before['operation_log']['rows'],after['operation_log']['rows'])
        self.assertEqual(item_status(f.f.item(after)),
            'EXCLUDED — shortcut policy; historical effects retained')

    def test_automatic_delivery_and_retry_do_not_rewrite_history_or_approvals(self):
        f=retired_fixture.RetiredAutomaticPublicationTests(methodName='runTest');f.setUp()
        self.addCleanup(f.doCleanups)
        exclude(f.f.j,'untracked');events=copy.deepcopy(f.f.j.events)
        self.assertEqual(f.publish()['result'],'PUBLICATION_VERIFIED')
        data=viewer.unpack(f.f.drive.content.decode());s=data['execution_sections']['sections'][0]
        self.assertEqual((s['status'],s['completed_file_count'],s['expected_file_count'],s['policy_excluded_file_count']),('COMPLETED',1,1,1))
        excluded=next(n for n in data['nodes'] if n.get('id')=='untracked')
        self.assertTrue(excluded['policy_excluded']);self.assertFalse(excluded.get('retired'))
        self.assertEqual(data['nodes'][1]['migration_summary']['policy_excluded_file_count'],1)
        row=f.f.sheet.matrices['Proposed Renames'][2]
        self.assertEqual(row[4:6],['No','=IF(E3="Yes","Approved","Declined")'])
        self.assertEqual(row[18],'EXCLUDED — shortcut policy; historical effects retained')
        self.assertEqual(f.f.j.events,events)
        self.assertEqual(f.publish()['result'],'PUBLICATION_VERIFIED')
        self.assertEqual((len(f.f.drive.writes),len(f.f.sheet.updates)),(1,1))

    def test_forged_exclusion_membership_is_rejected(self):
        from test_viewer_publication import fixture
        d,b=fixture();b['sections']['sections'][0].update(policy_excluded_file_ids=['file-a'],policy_excluded_file_count=1)
        with self.assertRaisesRegex(ValueError,'SECTION_POLICY_EXCLUSION_MEMBERSHIP_MISMATCH'):viewer.integrate(d,b)


class RetiredFolderDisplayTests(unittest.TestCase):
    def fixture(self):
        from test_viewer_publication import node
        data={'nodes':[node(0,-1,'Root',sid='root'),node(1,0,'Old category',sid='old-source'),
                       node(2,1,'2026'),node(3,2,'Deleted.mp4',False,'deleted')],
              'execution_sections':{'sections':[],'retired_empty_folders':[
                  {'source_id':'retired-destination','verified_at_utc':'2026-01-01T00:00:00Z',
                   'reason_code':'VERIFIED_EMPTY_PARENT_TRASHED'}]}}
        data['nodes'][1]['destination_overlay']={'destination_id':'retired-destination'}
        data['nodes'][3]['retired']=True
        return data

    def test_exact_retired_destination_hides_synthetic_deleted_branch_preserving_audit(self):
        d=self.fixture();before=copy.deepcopy(d['nodes']);viewer.mark_retired_empty_folders(d)
        self.assertEqual(d['retired_empty_folder_display']['hidden_ids'],['retired-destination'])
        self.assertFalse(d['nodes'][0].get('viewer_hidden'))
        for n in d['nodes'][1:]:self.assertTrue(n['viewer_hidden'])
        self.assertEqual([(n['id'],n['n']) for n in d['nodes']],[(n['id'],n['n']) for n in before])

    def test_active_child_protected_destination_pending_revision_and_wrong_identity_stay_visible(self):
        for change in ('active','protected','pending','wrong-identity','physical-child'):
            with self.subTest(change=change):
                d=self.fixture()
                if change=='active':d['nodes'][3].pop('retired')
                if change=='protected':d['execution_sections']['sections']=[{'destination_id':'retired-destination'}]
                if change=='pending':d['nodes'][1]['pending_revision']={'target_path':'Elsewhere'}
                if change=='wrong-identity':d['nodes'][1]['destination_overlay']['destination_id']='different'
                if change=='physical-child':d['nodes'][2]['id']='not-retired-child'
                viewer.mark_retired_empty_folders(d)
                self.assertFalse(any(n.get('viewer_hidden') for n in d['nodes']))

    def test_restored_proof_removal_clears_stale_hidden_flags(self):
        d=self.fixture();viewer.mark_retired_empty_folders(d)
        d['execution_sections']['retired_empty_folders']=[]
        viewer.mark_retired_empty_folders(d)
        self.assertFalse(any(n.get('viewer_hidden') or n.get('retired_empty_parent') for n in d['nodes']))

    def test_current_proposal_reusing_retired_path_is_protected(self):
        d=self.fixture()
        workbook={'sheets':{'Proposed Renames':[
            ['Source ID','Proposed name + type suffix','Proposed destination folder'],
            ['old-source','Old category','Root']]}}
        viewer.mark_retired_empty_folders(d,workbook)
        self.assertFalse(d['nodes'][1].get('viewer_hidden'))

    def test_renderer_guard_injection_is_idempotent(self):
        d=self.fixture();viewer.mark_retired_empty_folders(d)
        d.update(execution={'journal_sequence':1},notes=[])
        source="""<style></style><section class="execution-summary"></section><script>
function executionBadges(n){return n;}
const nodePair=n=>({folders:n.f?1:0,files:n.f?0:1,contexts:n.f&&!n.id?1:0});
match=N.map(n=>{const found=true;return found;});
function renderNode(i){const n=N[i];if(active&&!visible[i])return;const children=n.c.filter(c=>!active||visible[c]);}
</script>"""
        once=viewer.render(source,d);twice=viewer.render(once,d)
        self.assertEqual(once.split(),twice.split())
        for text in ('n.viewer_hidden||n.policy_excluded?zeroPair()',
                     'if(n.viewer_hidden)return false','if(n.viewer_hidden)return;',
                     '!N[c].viewer_hidden','Excluded shortcut · historical effects retained'):
            self.assertIn(text,once)


if __name__=='__main__':unittest.main()
