"""Saved no-op preflight is observation evidence, never an invented action."""
import copy
import unittest
from test_projection import MemoryJournal
from uqmc_recovery.journal import canonical, digest
from uqmc_recovery.publication_projection import build_publication, FOLDER
from uqmc_recovery import viewer_publication as viewer


class SatisfiedPreflightPublicationTests(unittest.TestCase):
    def make(self, *, raw_changes=None, target_changes=None, approved=True,
             member=True, cached=False, with_capture=True):
        self.j = MemoryJournal()
        self.before = {'id':'file', 'name':'Already correct.png', 'parents':['folder'],
            'mime_type':'image/png', 'trashed':False, 'parents_complete':True}
        self.folder = {'id':'folder', 'name':'Design Assets', 'parents':['old-root'],
            'mime_type':FOLDER, 'trashed':False, 'parents_complete':True}
        self.target = {**self.before, **(target_changes or {})}
        ops = [dict(operation_id='noop', file_id='file', action='MOVE', before=self.before,
                    target={k:self.target[k] for k in ('name','parents')}, depends_on=[]),
               dict(operation_id='folder-move', file_id='folder', action='MOVE', before=self.folder,
                    target={'parents':['destination']}, depends_on=[])]
        plan = {'run_id':'run', 'operations':ops}; self.ph=digest(canonical(plan))
        raw = {**self.before, **(raw_changes or {})}
        capture = {'metadata':{'file':raw} if with_capture else {},
                   'history_cached_source_ids':['file'] if cached else []}
        mapping = {'section_id':'section', 'input_sha256':'input', 'provenance_ref':self.j.evidence(capture)}
        projection = {'section_id':'section', 'input_sha256':'input',
            'members':[{'source_id':'file'}] if member else [],
            'destination_path_evidence_ref':self.j.evidence(mapping)}
        projection_hash=digest(canonical(projection))
        owner = {'section_id':'section', 'decision':'APPROVED', 'projection_sha256':projection_hash,
                 'member_source_ids':['file'] if member else []}
        owner_ref=self.j.evidence(owner)
        self.at='2026-10-21T09:59:00Z'
        imported = {'plan':plan, 'projection':projection, 'owner_approval_ref':owner_ref,
            'manifest':{'section_approval_validated':True, 'captured_at_utc':self.at}}
        self.j.append('SECTION_IMPORTED',payload={'proposal_hash':self.ph,
            'projection_sha256':projection_hash, 'owner_approval_ref':owner_ref,
            'manifest_ref':self.j.evidence(imported)})
        self.j.append('DRAFTED',payload={'run_id':'run', 'batch_id':'run', 'proposal_hash':self.ph, 'plan_ref':self.j.evidence(plan)})
        if approved:self.j.append('APPROVED',payload={'proposal_hash':self.ph,'approval_id':'approval'})
        self.j.append('DESIRED_APPLIED','desired-folder',1,{
            'source_id':'folder','run_id':'run','plan_sha256':self.ph,'direction':'FORWARD',
            'operation_order':2,'original_operation_ids':['folder-move'],'dependencies':[],
            'args':{'addParents':'destination','removeParents':'old-root'},
            'observed_ref':self.j.evidence({**self.folder,'parents':['destination']})})
        self.spec={'section_id':'section','title':'Design Assets','proposal_path':'Root/Design Assets',
            'plan_sha256':self.ph,'source_ids':['file'],'destination_id':'folder','destination_mode':'REUSED_MOVED'}

    def publish(self):
        return build_publication(self.j,allowed_source_ids=['file','folder'],
            identity_basis={'identity_basis_version':1,'source_ids':['file','folder']},sections=[self.spec])

    def test_satisfied_preflight_plus_inherited_move_completes_without_action_attribution(self):
        self.make();before=copy.deepcopy(self.j.events);out=self.publish()
        item=next(x for x in out['workbook']['items'] if x['source_id']=='file')
        self.assertEqual(out['sections']['sections'][0]['status'],'COMPLETED')
        self.assertEqual(item['observed']['metadata_verified_at_utc'],self.at)
        self.assertEqual(item['observed']['read_state'],'VERIFIED')
        self.assertEqual(item['location']['effect'],'INHERITED')
        for effect in ('rename','move'):
            self.assertEqual(item[effect]['effect'],'UNCHANGED')
            self.assertEqual(item[effect]['operation_ids'],[])
            self.assertIsNone(item[effect]['last_event_state'])
        self.assertFalse(any(op['source_id']=='file' for op in out['workbook']['operations']))
        self.assertEqual(len(out['operation_log']['rows']),1)
        self.assertEqual(before,self.j.events)

    def test_missing_unseen_cached_or_unapproved_evidence_does_not_complete(self):
        for kwargs in ({'with_capture':False},{'approved':False},{'member':False},{'cached':True}):
            with self.subTest(kwargs=kwargs):
                self.make(**kwargs)
                self.assertEqual(self.publish()['sections']['sections'][0]['completed_file_count'],0)

    def test_wrong_name_parent_id_and_trashed_preflight_do_not_complete(self):
        for changes in ({'name':'Changed.png'},{'parents':['other']},{'id':'other'},{'trashed':True}):
            with self.subTest(changes=changes):
                self.make(raw_changes=changes)
                self.assertEqual(self.publish()['sections']['sections'][0]['completed_file_count'],0)

    def test_changed_approved_target_requires_actual_action_or_readback(self):
        self.make(target_changes={'name':'New target.png'})
        self.assertEqual(self.publish()['sections']['sections'][0]['completed_file_count'],0)

    def test_later_uncertain_conflict_or_prepared_action_is_not_overridden(self):
        for kind,fields in [('STOPPED',{'state':'UNCERTAIN'}),('STOPPED',{'state':'CONFLICT'}),('PREPARED',{})]:
            with self.subTest(kind=kind,fields=fields):
                self.make()
                self.j.append('DESIRED_'+kind,'desired-file',1,{
                    'source_id':'file','run_id':'run','plan_sha256':self.ph,'direction':'FORWARD',
                    'operation_order':1,'original_operation_ids':['noop'],'dependencies':[],**fields})
                self.assertEqual(self.publish()['sections']['sections'][0]['completed_file_count'],0)

    def test_legacy_uncertain_request_is_not_cleared(self):
        self.make();self.j.append('UNCERTAIN','noop',1,{'file_id':'file','run_id':'run',
            'action':'MOVE','direction':'FORWARD','reason_code':'REQUEST_NOT_PROVEN_TERMINAL'})
        out=self.publish();self.assertEqual(out['sections']['sections'][0]['completed_file_count'],0)
        item=next(x for x in out['workbook']['items'] if x['source_id']=='file')
        self.assertIn('REQUEST_NOT_PROVEN_TERMINAL',item['reason_codes'])

    def test_tampered_plan_authority_fails_closed(self):
        self.make()
        e=self.j.events[0];value=self.j.evidence_values[e['payload']['manifest_ref']]
        value['plan']['operations'][0]['target']['name']='Unapproved.png'
        with self.assertRaisesRegex(ValueError,'SATISFIED_PREFLIGHT_AUTHORITY_MISMATCH'):self.publish()


class RepeatedViewerCheckpointTests(unittest.TestCase):
    def test_every_render_updates_header_embedded_and_summary_checkpoint(self):
        source='<html><style></style><p>Version 20 · Updated yesterday</p><script id="tree-data"></script><section class="execution-summary"></section></html>'
        for checkpoint in (2658,6751,7000):
            data={'execution':{'journal_sequence':checkpoint},'execution_sections':{'sections':[]}}
            source=viewer.render(source,data)
            self.assertIn('Execution update · Journal checkpoint '+str(checkpoint)+'</p>',source)
            self.assertIn('Verified destination status from journal checkpoint '+str(checkpoint),source)
            self.assertEqual(viewer.unpack(source)['execution']['journal_sequence'],checkpoint)
            for previous in (2658,6751):
                if previous!=checkpoint:self.assertNotIn('Journal checkpoint '+str(previous)+'</p>',source)

if __name__=='__main__':unittest.main()
