"""Synthetic explicit empty-parent cleanup; never uses live Drive."""
import copy
from pathlib import Path
import tempfile
import unittest

from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, digest
from uqmc_recovery.direct_transport import GoogleDriveTransport, TransportError
from uqmc_recovery.revision_cleanup import cleanup_run, retired_parents, filter_registry, candidates, PREFIX
from test_direct_transport import Service


def raw(sid, name, parents):
    return dict(id=sid,name=name,parents=parents,mimeType=FOLDER,trashed=False,capabilities={'canTrash':True})


class Death(BaseException):pass


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.j=Journal(Path(self.tmp.name)/'journal')
        self.old=raw('old','Category',['root']);self.before=raw('moved','Year',['old'])
        self.after=raw('moved','Category',['new'])
        plan={'run_id':'fixture-run','operations':[
            dict(operation_id='rename',action='RENAME',file_id='moved',before=self.before,target={'name':'Category'},depends_on=[]),
            dict(operation_id='move',action='MOVE',file_id='moved',before={**self.before,'name':'Category'},target={'parents':['new']},depends_on=['rename'])]}
        self.plan_hash=digest(plan)
        self.workbook={'sheets':{'Proposed Renames':[['Source ID'],['moved']]}}
        with self.j.locked():
            self.j.append('DRAFTED',payload={'proposal_hash':self.plan_hash,'plan_ref':self.j.evidence(plan)})
            self.j.append('DESIRED_APPLIED','desired',payload={'source_id':'moved','plan_sha256':self.plan_hash,
                'before_ref':self.j.evidence(self.before),'target_ref':self.j.evidence(self.after),'observed_ref':self.j.evidence(self.after)})
            metadata=self.j.evidence({'metadata':{'old':self.old,'moved':self.before,'root':raw('root','Root',[]),'new':raw('new','Year',['root'])}})
            binding=self.j.evidence({'schema_version':'uqmc.destination.revision-frozen.v1',
                'plan':plan,'manifest':{'items':[]},'workbook_export':self.workbook})
        self.prepared=dict(run_id='fixture-run',plan_sha256=self.plan_hash,section_id='section',source_metadata_ref=metadata,
            revision_origin_ref=binding,prior_destination_paths={'Section/Category':'old'},destination_paths={'Section':'root','Section/Year':'new'},excluded_ids=[])
        self.registry={'sections':[{'publication_spec':{'section_id':'section','destination_id':'root','destination_paths':{'old':'Section/Category','root':'Section'},'destination_ids':['root','old']},'destination_paths':{'Section':'root','Section/Category':'old'}}], 'runs':[self.prepared]}
        self.files={'old':copy.deepcopy(self.old),'root':raw('root','Any root label',[])};self.children=[];self.writes=[];self.mode=None

    def factory(self, allowed, mutations, excluded, apply):
        outer=self
        class Transport:
            def get_file(self,sid):
                assert sid in allowed and sid not in excluded
                if sid=='root' and outer.mode=='ancestry_unavailable':
                    raise TransportError('DRIVE_REQUEST_TIMEOUT',outcome='READ_FAILED')
                return copy.deepcopy(outer.files[sid])
            get_trash_candidate=get_file
            def list_children(self,sid):return copy.deepcopy(outer.children)
            def trash_file(self,sid):
                assert apply and sid in mutations
                assert outer.j.records()[-1]['kind']==PREFIX+'ATTEMPTED'
                outer.writes.append(sid)
                if outer.mode=='before_death':raise Death()
                if outer.mode=='refused':raise TransportError('DRIVE_HTTP_ERROR',status=403,outcome='KNOWN_NOT_APPLIED')
                outer.files[sid]['trashed']=True
                if outer.mode=='death':raise Death()
                if outer.mode=='lost':raise TransportError('DRIVE_REQUEST_TIMEOUT',outcome='UNKNOWN')
                return copy.deepcopy(outer.files[sid])
        return Transport()

    def run_cleanup(self,apply=True):
        return cleanup_run(self.j,self.prepared,self.registry,self.workbook,self.factory,apply=apply,
                           protected_ids={'root'},new_tree_root_id='root')

    def test_preview_no_write_then_empty_verified_and_registry_history_preserved(self):
        seq=len(self.j.records());self.assertEqual(self.run_cleanup(False)['counts'],{'READY':1});self.assertEqual(len(self.j.records()),seq)
        self.assertEqual(self.run_cleanup()['counts'],{'TRASHED':1})
        self.assertEqual(self.writes,['old']);retired=retired_parents(self.j)
        self.assertEqual(retired['old']['name'],'Category')
        registry=filter_registry(self.j,self.registry)
        self.assertEqual(registry['runs'],self.registry['runs'])
        self.assertEqual(registry['sections'][0]['destination_paths'],{'Section':'root'})
        self.assertIn('Section/Category',self.registry['sections'][0]['destination_paths'])

    def test_repeat_has_no_second_write(self):
        self.run_cleanup();self.assertEqual(self.run_cleanup()['counts'],{'ALREADY_RETIRED':1});self.assertEqual(self.writes,['old'])

    def test_nonempty_skipped(self):
        self.children=[{'id':'unrelated'}];self.assertEqual(self.run_cleanup()['counts'],{'SKIPPED':1});self.assertFalse(self.writes)

    def test_capability_denied_skipped(self):
        self.files['old']['capabilities']['canTrash']=False
        self.assertEqual(self.run_cleanup()['items'][0]['reason'],'TRASH_CAPABILITY_UNAVAILABLE');self.assertFalse(self.writes)

    def test_actual_name_or_parent_drift_skipped(self):
        for field,value in [('name','Manual title'),('parents',['other'])]:
            self.files['old']=copy.deepcopy(self.old);self.files['old'][field]=value
            self.assertEqual(self.run_cleanup()['items'][0]['reason'],'PARENT_IDENTITY_DRIFT')
        self.assertFalse(self.writes)

    def test_future_source_assignment_is_protected(self):
        self.workbook['sheets']['Proposed Renames'].append(['old'])
        self.assertEqual(self.run_cleanup()['candidate_count'],0)

    def test_current_target_and_other_section_root_are_protected(self):
        self.registry['sections'].append({'publication_spec':{'section_id':'other','destination_id':'old'}})
        self.assertEqual(self.run_cleanup()['candidate_count'],0)

    def test_excluded_identity_protected(self):
        self.prepared['excluded_ids']=['old'];self.assertEqual(self.run_cleanup()['candidate_count'],0)

    def test_unverified_run_blocks_cleanup(self):
        with self.j.locked():
            self.j.append('DESIRED_OBSERVED','observed',payload={'plan_sha256':self.plan_hash,'source_id':'moved','state':'NOT_APPLIED','observed_ref':self.j.evidence(self.before)})
        with self.assertRaisesRegex(Exception,'CLEANUP_RUN_NOT_VERIFIED_COMPLETE'):self.run_cleanup()
        self.assertFalse(self.writes)

    def test_response_lost_reconciles_once(self):
        self.mode='lost';self.assertEqual(self.run_cleanup()['counts'],{'TRASHED':1});self.assertEqual(self.writes,['old'])
        self.assertTrue(retired_parents(self.j)['old']['reconciled'])

    def test_crash_after_success_resume_reconciles_no_retry(self):
        self.mode='death'
        with self.assertRaises(Death):self.run_cleanup()
        self.mode=None
        self.assertEqual(self.run_cleanup()['counts'],{'RECONCILED_TRASHED':1});self.assertEqual(self.writes,['old'])

    def test_crash_before_remote_outcome_unknown_never_retries(self):
        self.mode='before_death'
        with self.assertRaises(Death):self.run_cleanup()
        self.mode=None
        self.assertEqual(self.run_cleanup()['counts'],{'UNCERTAIN':1});self.assertEqual(self.writes,['old'])

    def test_refusal_does_not_retry(self):
        self.mode='refused';self.assertEqual(self.run_cleanup()['counts'],{'UNCERTAIN':1})
        self.mode=None;self.assertEqual(self.run_cleanup()['counts'],{'UNCERTAIN':1});self.assertEqual(self.writes,['old'])

    def test_unknown_parent_not_broadened(self):
        with self.j.locked():self.prepared['source_metadata_ref']=self.j.evidence({'metadata':{'moved':self.before}})
        self.assertEqual(self.run_cleanup()['candidate_count'],0)


class TrashTransportTests(unittest.TestCase):
    def test_exact_trash_body_single_send(self):
        service=Service({'id':'old','trashed':True})
        transport=GoogleDriveTransport(service,allowed_ids={'old'},mutation_ids={'old'},apply=True)
        transport.trash_file('old')
        self.assertEqual(service.calls[0][0],'files.update')
        self.assertEqual(service.calls[0][1]['body'],{'trashed':True})
        self.assertEqual(service.calls[1],('execute',0))

    def test_readonly_and_unapproved_never_write(self):
        service=Service();transport=GoogleDriveTransport(service,allowed_ids={'old'},mutation_ids=set(),apply=False)
        with self.assertRaisesRegex(TransportError,'READ_ONLY'):transport.trash_file('old')
        transport.apply_enabled=True
        with self.assertRaisesRegex(TransportError,'OUTSIDE_APPROVED_MUTATION_IDS'):transport.trash_file('old')
        self.assertFalse(service.calls)

    def test_capability_read_requests_only_cleanup_fields(self):
        service=Service({'id':'old'});transport=GoogleDriveTransport(service,allowed_ids={'old'},apply=False)
        transport.get_trash_candidate('old')
        self.assertIn('capabilities(canTrash)',service.calls[0][1]['fields'])
        self.assertEqual(service.calls[0][0],'files.get')

class CleanupLifecycleTests(CleanupTests):
    def set_old_parent(self,parents):
        self.files['old']['parents']=parents
        with self.j.locked():
            metadata=self.j.read_evidence(self.prepared['source_metadata_ref'])
            metadata['metadata']['old']['parents']=parents
            self.prepared['source_metadata_ref']=self.j.evidence(metadata)

    def test_nested_id_ancestry_proof_is_recorded(self):
        self.set_old_parent(['middle'])
        self.files['middle']=raw('middle','Unrelated display name',['root'])
        self.assertEqual(self.run_cleanup()['counts'],{'TRASHED':1})
        event=next(e for e in self.j.records() if e['kind']==PREFIX+'ATTEMPTED')
        proof=self.j.read_evidence(event['payload']['ancestry_ref'])
        self.assertEqual(proof['root_id'],'root')
        self.assertEqual([x['id'] for x in proof['chain']],['old','middle','root'])

    def test_same_logical_path_outside_new_root_does_not_authorize_trash(self):
        self.set_old_parent(['other-root'])
        self.files['other-root']=raw('other-root','New File Structure',[])
        result=self.run_cleanup()
        self.assertEqual(result['items'][0]['reason'],'PARENT_NOT_PROVEN_INSIDE_NEW_TREE')
        self.assertFalse(self.writes)

    def test_fresh_ancestor_location_overrides_old_snapshot_and_path_labels(self):
        self.set_old_parent(['middle'])
        with self.j.locked():
            metadata=self.j.read_evidence(self.prepared['source_metadata_ref'])
            metadata['metadata']['middle']=raw('middle','Section',['root'])
            self.prepared['source_metadata_ref']=self.j.evidence(metadata)
        self.files['middle']=raw('middle','Section',['other-root'])
        self.files['other-root']=raw('other-root','New File Structure',[])
        self.assertEqual(self.run_cleanup()['items'][0]['reason'],'PARENT_NOT_PROVEN_INSIDE_NEW_TREE')
        self.assertFalse(self.writes)

    def test_cycle_multiple_parents_and_unavailable_ancestry_never_trash(self):
        self.set_old_parent(['old'])
        with self.assertRaisesRegex(Exception,'CLEANUP_PARENT_CYCLE'):self.run_cleanup()
        self.set_old_parent(['root','other'])
        self.assertEqual(self.run_cleanup()['items'][0]['reason'],'PARENT_NOT_PROVEN_INSIDE_NEW_TREE')
        self.set_old_parent(['root']);self.mode='ancestry_unavailable'
        self.assertEqual(self.run_cleanup()['items'][0]['reason'],'NEW_TREE_ANCESTRY_UNAVAILABLE')
        self.assertFalse(self.writes)

    def test_configured_root_and_missing_root_are_protected(self):
        result=cleanup_run(self.j,self.prepared,self.registry,self.workbook,self.factory,apply=True,new_tree_root_id='old')
        self.assertEqual(result['candidate_count'],0)
        result=cleanup_run(self.j,self.prepared,self.registry,self.workbook,self.factory,apply=True)
        self.assertEqual(result['reason'],'NEW_TREE_ROOT_NOT_CONFIGURED');self.assertFalse(self.writes)

    def test_direct_cleanup_cannot_bypass_explicit_revision_requirement(self):
        self.prepared.pop('revision_origin_ref')
        self.assertEqual(self.run_cleanup()['reason'],'EXPLICIT_REARCHITECTURE_REQUIRED')
        self.assertFalse(self.writes)

    def test_restored_parent_is_observed_and_not_retrashed(self):
        self.run_cleanup();self.files['old']['trashed']=False
        self.assertEqual(self.run_cleanup()['counts'],{'RESTORED':1})
        self.assertFalse(retired_parents(self.j));self.assertEqual(self.writes,['old'])
        self.assertEqual(self.run_cleanup()['counts'],{'RESTORED':1});self.assertEqual(self.writes,['old'])

    def test_restore_with_changed_identity_not_accepted(self):
        self.run_cleanup();self.files['old']['trashed']=False;self.files['old']['name']='Manual rename'
        self.assertEqual(self.run_cleanup()['items'][0]['reason'],'PARENT_IDENTITY_DRIFT')
        self.assertIn('old',retired_parents(self.j))

    def test_fresh_future_destination_path_protects_empty_folder(self):
        self.prepared['destination_paths']['Section/Category']='old'
        self.workbook['sheets']['Proposed Moves']=[['Proposed target path'],['Section/Category/Future']]
        self.assertEqual(self.run_cleanup()['candidate_count'],0)

    def test_only_explicit_retirement_releases_stale_target_path(self):
        from uqmc_recovery.importer import RETIRED_MARKER
        self.prepared['destination_paths']['Section/Category']='old'
        self.workbook['sheets']['Proposed Renames']=[['Source ID','Proposed destination folder','Evidence / rationale','Decision (Yes/No)'],
            ['deleted','Section/Category','', 'No']]
        self.assertEqual(self.run_cleanup()['candidate_count'],0)
        self.workbook['sheets']['Proposed Renames'][1][2]=RETIRED_MARKER
        self.assertEqual(self.run_cleanup()['counts'],{'TRASHED':1})

    def test_success_hook_skips_failure_rollback_stale_and_unavailable_sheet(self):
        from uqmc_recovery.revision_cleanup import after_success
        from unittest.mock import patch
        capture={**self.workbook,'source_revision':{'version':'1'}}
        cases=[({'FAILED':1},False,None,lambda:{'version':'1'}),
               ({'APPLIED':1},True,None,lambda:{'version':'1'}),
               ({'APPLIED':1},False,'stopped',lambda:{'version':'1'}),
               ({'APPLIED':1},False,None,lambda:{'version':'2'}),
               ({'APPLIED':1},False,None,lambda:1/0)]
        for counts,rollback,guard,revision in cases:
            with patch('uqmc_recovery.revision_cleanup.cleanup_run') as run:
                result=after_success(self.j,self.prepared,self.registry,capture,self.factory,
                    result={'counts':counts},rollback=rollback,guard_reason=guard,current_revision=revision,new_tree_root_id='root')
                self.assertEqual(result['state'],'SKIPPED');run.assert_not_called()

    def test_ordinary_success_does_not_call_cleanup_or_fetch_sheet(self):
        from uqmc_recovery.revision_cleanup import after_success
        from unittest.mock import Mock
        self.prepared.pop('revision_origin_ref')
        capture={**self.workbook,'source_revision':{'version':'1'}}
        factory=Mock(side_effect=AssertionError('No Drive reads for normal migration cleanup'))
        refresh=Mock(side_effect=AssertionError('No sheet read for skipped cleanup'))
        result=after_success(self.j,self.prepared,self.registry,capture,factory,
            result={'counts':{'APPLIED':1}},rollback=False,guard_reason=None,current_revision=refresh,new_tree_root_id='root')
        self.assertEqual(result['reason'],'EXPLICIT_REARCHITECTURE_REQUIRED')
        factory.assert_not_called();refresh.assert_not_called();self.assertFalse(self.writes)


class DestinationCleanupHookTests(unittest.TestCase):
    def test_ordinary_command_never_invokes_cleanup_hook(self):
        from unittest.mock import patch
        import test_destination_command as fixture
        f=fixture.DestinationCommandTests();f.setUp();self.addCleanup(f.doCleanups);f.mixed()
        def cleanup(j,*a,**kw):
            self.assertFalse(kw['rollback']);self.assertEqual(kw['result']['counts'],{'APPLIED':1})
            j.append('CLEANUP_FIXTURE_AUDIT_ONLY',payload={})
            return {'counts':{'SKIPPED':1},'items':[]}
        with patch('uqmc_recovery.revision_cleanup.after_success',side_effect=cleanup) as hook:
            result=f.run_command(apply=True)
        hook.assert_not_called();self.assertEqual(result['cleanup']['reason'],'EXPLICIT_REARCHITECTURE_REQUIRED')
        self.assertEqual(result['journal_sequence'],len(Journal(f.config['store']).records()))
        self.assertEqual((Path(result['backup'])/'events.jsonl').read_bytes(),Journal(f.config['store']).path.read_bytes())
        self.assertEqual(len(f.publications),1)

    def test_revision_hook_and_rollback_restore_gate(self):
        from unittest.mock import patch
        import test_destination_revision as fixture
        f=fixture.DestinationRevisionTests();f.setUp();self.addCleanup(f.doCleanups)
        from uqmc_recovery.revision_cleanup import after_success
        with patch('uqmc_recovery.revision_cleanup.after_success',wraps=after_success) as hook:
            result=f.run_revision(apply=True)
        hook.assert_called_once();self.assertIn('cleanup',result)
        parents=set(f.before['parents']);writes=len(f.f.server.requests)
        with patch('uqmc_recovery.revision_cleanup.retired_parent_ids',return_value=parents), patch('uqmc_recovery.revision_cleanup.reconcile_restored') as restore:
            with self.assertRaisesRegex(Exception,'ROLLBACK_REQUIRES_RESTORE_TRASHED_PARENTS'):
                f.f.run_command(apply=True,rollback=result['run_id'])
        restore.assert_called_once();self.assertEqual(len(f.f.server.requests),writes)
