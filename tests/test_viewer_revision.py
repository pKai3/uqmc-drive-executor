"""Revision display follows verified stable IDs, not future path strings."""
import copy
import unittest
from test_projection import MemoryJournal
import test_publication_projection as projection_fixture
from uqmc_recovery import destination_publication as publication
from uqmc_recovery import viewer_publication as viewer
from uqmc_recovery.publication_projection import FOLDER


def tree(created=False):
    def node(i,p,name,folder=True,sid=''):
        return dict(i=i,p=p,n=name,o=None,f=folder,id=sid,s='destination',c=[],changed=False,review=False)
    nodes=[node(0,-1,'Root'),node(1,0,'General'),node(2,1,'2024',sid='old-context' if created else 'folder'),node(3,2,'File.pdf',False,'file')]
    if created:nodes[2]['destination_overlay']={'destination_id':'folder','destination_url':'https://drive.google.com/drive/folders/folder','mode':'CREATED'}
    return {'nodes':nodes,'roots':{'destination':[0],'retained':[]},'notes':[],'stats':{}}


def revision(verified):
    return {'source_id':'folder','before_path':'Root/General/2024',
            'target_path':'Root/2024/Minutes','target_name':'Minutes','verified':verified}


def workbook(created=False):
    rows=[['Source ID','Proposed name + type suffix','Proposed destination folder'],
          ['file','File.pdf','Root/2024/Minutes']]
    if not created:rows.append(['folder','Minutes','Root/2024'])
    return {'exported_at_utc':'2026-10-22T00:00:00Z','sheets':{'Proposed Renames':rows}}


class RevisionTreeTests(unittest.TestCase):
    def test_pending_keeps_current_tree_and_links_and_labels_future_path(self):
        for created in (False,True):
            with self.subTest(created=created):
                data=tree(created);before=copy.deepcopy(data['nodes'][2].get('destination_overlay'))
                viewer.refresh_proposal(data,workbook(created),folder_revisions=[revision(False)])
                n=data['nodes'][2]
                self.assertEqual((n['n'],n['p']),('2024',1))
                self.assertEqual(n.get('destination_overlay'),before)
                self.assertEqual(n['pending_revision']['target_path'],'Root/2024/Minutes')
                self.assertEqual(data['nodes'][3]['p'],2)
                self.assertEqual(len(data['nodes']),5)

    def test_verified_revision_reparents_same_node_and_children_repeatedly(self):
        for created in (False,True):
            with self.subTest(created=created):
                data=tree(created);ids={n['id'] for n in data['nodes'] if n['id']}
                for _ in range(2):
                    viewer.refresh_proposal(data,workbook(created),folder_revisions=[revision(True)])
                    folder=viewer.resolve_folder_path(data,'Root/2024/Minutes')
                    child=next(n for n in data['nodes'] if n['id']=='file')
                    self.assertEqual(child['p'],folder['i'])
                    self.assertNotIn('pending_revision',folder)
                    self.assertEqual({n['id'] for n in data['nodes'] if n['id']},ids)
                    if created:self.assertEqual(folder['destination_overlay']['destination_id'],'folder')

    def test_pending_and_verified_created_folder_links_survive_full_integration(self):
        for verified in (False, True):
            with self.subTest(verified=verified):
                data=tree(True)
                viewer.refresh_proposal(data,workbook(True),folder_revisions=[revision(verified)])
                def dest(sid,name,parent):
                    return {'id':sid,'name':name,'parent_ids':[parent],
                            'destination_link':'https://drive.google.com/drive/folders/'+sid}
                destinations=[dest('general','General','root'),dest('year','2024','root'),
                              dest('folder','Minutes' if verified else '2024','year' if verified else 'general')]
                section={'section_id':'section','title':'Root','path':'Root','destination_id':'root',
                    'destination_url':'https://drive.google.com/drive/folders/root','mode':'CREATED',
                    'expected_file_count':1,'completed_file_count':1,'status':'COMPLETED','verified_file_ids':['file'],
                    'created_folder_count':3,'created_destinations':destinations,'retained_source_folder_ids':[]}
                public_items=[{'source_id':sid,'observed':{},'location':{}} for sid in ('old-context','file')]
                bundle={'html':{'snapshot_id':'same','journal_sequence':7,'items':public_items},
                    'sections':{'snapshot_id':'same','journal_sequence':7,'sections':[section]},
                    'workbook':{'items':[{'source_id':'file','observed':{'read_state':'VERIFIED','parent_ids':['folder']},
                                         'location':{'effect':'INHERITED'}}]}}
                viewer.integrate(data,bundle)
                folder=next(n for n in data['nodes'] if n['id']=='old-context')
                self.assertEqual(folder['destination_overlay']['destination_id'],'folder')
                self.assertEqual(folder['n'],'Minutes' if verified else '2024')
                self.assertEqual('pending_revision' in folder,not verified)
                self.assertEqual(viewer.resolve_folder_path(data,'Root/2024')['destination_overlay']['destination_id'],'year')

    def test_unbound_changed_folder_stays_blocked(self):
        with self.assertRaisesRegex(ValueError,'FOLDER_PROPOSAL_CHANGED'):
            viewer.refresh_proposal(tree(),workbook())

    def test_other_identity_at_target_is_not_overwritten(self):
        data=tree();data['nodes'] += [dict(data['nodes'][1],i=4,p=0,n='2024',id='other-year'),dict(data['nodes'][2],i=5,p=4,n='Minutes',id='other-folder')]
        with self.assertRaisesRegex(ValueError,'REVISION_VIEWER_TARGET_COLLISION'):
            viewer.refresh_proposal(data,workbook(),folder_revisions=[revision(True)])

    def test_wrong_prior_path_same_id_is_rejected(self):
        bad={**revision(False),'before_path':'Root/Other/2024'}
        with self.assertRaisesRegex(ValueError,'REVISION_VIEWER_PATH_ID_MISMATCH'):
            viewer.refresh_proposal(tree(),workbook(),folder_revisions=[bad])


class RevisionEvidenceTests(unittest.TestCase):
    def make(self):
        self.j=MemoryJournal();self.before={'id':'folder','name':'2024','parents':['general'],'mimeType':FOLDER,'trashed':False}
        self.target={**self.before,'name':'Minutes','parents':['year']}
        plan={'operations':[{'file_id':'folder','action':'MOVE','target':{'parents':['year']}}]}
        self.ph=publication._sha(publication._bytes(plan))
        manifest={'items':[{k:v for k,v in revision(False).items() if k!='verified'}]}
        owner={'decision':'APPROVED','plan_sha256':self.ph,'manifest_sha256':publication._sha(publication._bytes(manifest)),'section_id':'section'}
        owner_ref=self.j.evidence(owner)
        evidence={'schema_version':'uqmc.destination.revision-frozen.v1','plan':plan,'manifest':manifest,
          'owner_approval_ref':owner_ref,'source_metadata_ref':self.j.evidence({'metadata':{'folder':self.before}}),
          'prior_destination_paths':{'Root/General/2024':'folder'},
          'destination_paths':{'Root/2024/Minutes':'folder','Root/2024':'year'}}
        self.j.append('DESTINATION_REVISION_FROZEN',payload={'section_id':'section','proposal_hash':self.ph,
            'owner_approval_ref':owner_ref,'manifest_ref':self.j.evidence(evidence)})

    def observe(self,kind='APPLIED',**extra):
        p={'source_id':'folder','plan_sha256':self.ph,'observed_ref':self.j.evidence(self.target),**extra}
        self.j.append('DESIRED_'+kind,payload=p)

    def test_freeze_is_pending_until_exact_new_plan_readback(self):
        self.make();self.assertFalse(publication.revision_folder_updates(self.j)[0]['verified'])
        self.observe();self.assertTrue(publication.revision_folder_updates(self.j)[0]['verified'])
        self.observe('STOPPED',state='UNCERTAIN',observed_ref=None)
        self.assertFalse(publication.revision_folder_updates(self.j)[0]['verified'])

    def test_other_plan_or_different_target_does_not_verify(self):
        for extra in ({'plan_sha256':'unrelated'}, {'observed_ref':'wrong'}):
            self.make()
            if extra.get('observed_ref')=='wrong':extra={'observed_ref':self.j.evidence(self.before)}
            self.observe(**extra)
            self.assertFalse(publication.revision_folder_updates(self.j)[0]['verified'])

    def test_evidence_plan_mismatch_is_rejected(self):
        self.make();self.j.events[0]['payload']['proposal_hash']='wrong'
        with self.assertRaisesRegex(ValueError,'VIEWER_REVISION_EVIDENCE_MISMATCH'):
            publication.revision_folder_updates(self.j)


class CreatedDestinationRevisionTests(unittest.TestCase):
    def test_later_created_folder_revision_readback_wins_over_creation(self):
        f=projection_fixture.PublicationTests();f.setUp();j=f.j
        folder={'id':'destination','name':'Old folder','parents':['old-parent'],'mime_type':FOLDER}
        authority={'decision':'APPROVED','folders':[{'key':'key','name':'Old folder'}]}
        j.append('REQUIRED_FOLDER_VERIFIED','created',payload={'section_id':'section','key':'key','file_id':'destination',
            'name':'Old folder','parent_id':'old-parent','authority_ref':j.evidence(authority),'observed_ref':j.evidence(folder)})
        f.freeze([{'operation_id':'rename-folder','file_id':'destination','action':'RENAME',
            'before':folder,'target':{'name':'New folder'},'depends_on':[]}])
        spec={'section_id':'section','title':'New folder','proposal_path':'Root/New folder','plan_sha256':f.ph,
              'source_ids':[],'destination_id':'destination','destination_mode':'CREATED'}
        self.assertNotEqual(f.publish(sections=[spec])['sections']['sections'][0]['status'],'COMPLETED')
        raw={**folder,'name':'New folder'}
        j.append('DESIRED_APPLIED','revision',1,{'source_id':'destination','run_id':'run','plan_sha256':f.ph,'direction':'FORWARD',
            'operation_order':1,'original_operation_ids':['rename-folder'],'dependencies':[],
            'args':{'name':'New folder'},'observed_ref':j.evidence(raw)})
        spec={'section_id':'section','title':'New folder','proposal_path':'Root/New folder','plan_sha256':f.ph,
              'source_ids':[],'destination_id':'destination','destination_mode':'CREATED'}
        out=f.publish(sections=[spec]);dest=out['sections']['sections'][0]['verified_destinations'][0]
        self.assertEqual(dest['name'],'New folder')
        self.assertEqual(dest['mode'],'CREATED')
        item=f.item(out,'destination');self.assertEqual(item['observed']['exact_name'],'New folder')
        j.append('DESIRED_STOPPED','revision',2,{'source_id':'destination','run_id':'run','plan_sha256':f.ph,'direction':'FORWARD',
            'operation_order':1,'original_operation_ids':['rename-folder'],'dependencies':[],'state':'UNCERTAIN'})
        out=f.publish(sections=[spec]);self.assertNotEqual(out['sections']['sections'][0]['status'],'COMPLETED')
        self.assertEqual(out['sections']['sections'][0]['verified_destinations'],[])

if __name__=='__main__':unittest.main()
