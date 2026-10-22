"""Offline approved-name policy: accept the initial title, never erase recovery."""
import copy
import io
import unittest

import test_destination_command as fixtures
from uqmc_recovery.destination_history import reconcile_item
from uqmc_recovery.destination_console import ConsoleReporter
from uqmc_recovery.journal import Journal


def core(name='Original.pdf', **extra):
    return {'id':'file','name':name,'parents':['parent'],'mime_type':'application/pdf',
            'md5_checksum':'unchanged',**extra}


class StartingNameDriftTests(unittest.TestCase):
    def fixture(self):
        f=fixtures.DestinationCommandTests(methodName='runTest')
        f.setUp();self.addCleanup(f.doCleanups)
        return f

    def test_explicit_pending_rename_warns_and_preserves_original_evidence(self):
        baseline=core();live=core('Actual starting.pdf');target=core('Approved.pdf')
        before=copy.deepcopy((baseline,live,target))
        strict=reconcile_item(None,target,live,baseline=baseline)
        self.assertEqual(strict['reason'],'UNAPPROVED_CURRENT_STATE:name')
        accepted=reconcile_item(None,target,live,baseline=baseline,explicit_rename=True)
        self.assertEqual(accepted['disposition'],'READY')
        self.assertEqual(accepted['pending_fields'],['name'])
        self.assertEqual(accepted['warnings'],[{'reason_code':'STARTING_NAME_DRIFT_ACCEPTED',
            'original_name':'Original.pdf','observed_name':'Actual starting.pdf',
            'approved_target_name':'Approved.pdf','rollback_before_name':'Actual starting.pdf'}])
        self.assertEqual((baseline,live,target),before)

    def test_identity_parent_content_and_authority_checks_remain_strict(self):
        for changes,kwargs in [({'id':'wrong'},{}),({'parents':['elsewhere']},{}),
            ({'mime_type':'text/plain'},{}),({'md5_checksum':'changed'},{}),
            ({'trashed':True},{}),({}, {'approved':False}),({}, {'excluded':True})]:
            with self.subTest(changes=changes,kwargs=kwargs):
                result=reconcile_item(None,core('Approved.pdf'),core('Different.pdf',**changes),
                    baseline=core(),explicit_rename=True,**kwargs)
                self.assertEqual(result['disposition'],'BLOCKED')

    def test_completed_and_rolled_back_names_are_not_reopened(self):
        history={'original':core(),'baseline':core(),'states':[core()],
            'completed_fields':{'name':'Approved.pdf'}}
        for current,target in [(core('Manual edit.pdf'),core('Approved.pdf')),
                               (core('Approved.pdf'),core('New target.pdf'))]:
            result=reconcile_item(history,target,current,explicit_rename=True)
            self.assertEqual(result['disposition'],'BLOCKED')
            self.assertTrue(result['reason'].startswith('COMPLETED_EFFECT'))
        history['completed_fields']={};history['rolled_back_fields']=['name']
        result=reconcile_item(history,core('Approved.pdf'),core(),explicit_rename=True)
        self.assertEqual(result['reason'],'ROLLED_BACK_EFFECT_REQUIRES_EXPLICIT_RESUME')

    def test_pending_request_keeps_recovery_and_target_change_protection(self):
        request={'direction':'FORWARD','before':core(),'target':core('Approved.pdf')}
        history={'original':core(),'baseline':core(),'states':[core(),core('Approved.pdf')],
            'pending_requests':[request]}
        blocked=reconcile_item(history,core('Approved.pdf'),core('Manual edit.pdf'),explicit_rename=True)
        self.assertEqual(blocked['reason'],'UNAPPROVED_CURRENT_STATE:name')
        target=reconcile_item(history,core('Approved.pdf'),core('Approved.pdf'),explicit_rename=True)
        self.assertEqual(target['disposition'],'RECOVER_AT_TARGET')
        self.assertTrue(target['recovery_required'])
        changed=reconcile_item(history,core('Different target.pdf'),core(),explicit_rename=True)
        self.assertEqual(changed['reason'],'PENDING_REQUEST_TARGET_CHANGED_RECONCILE_EXISTING_PLAN')

    def test_preview_warns_without_mutation_and_collision_still_blocks(self):
        f=self.fixture();f.server.drive.files['leaf']['name']='Actual starting.pdf'
        before=copy.deepcopy(f.export)
        preview=f.run_command()
        self.assertEqual(preview['blockers'],[])
        self.assertEqual(preview['warnings'][0]['observed_name'],'Actual starting.pdf')
        self.assertEqual(f.export,before)
        self.assertEqual(f.server.requests,[])
        self.assertEqual(Journal(f.config['store']).records(),[])
        f.server.drive.files['collision']=fixtures.raw('collision','Approved.pdf',['source-root'])
        # Keep the source root intact by assigning the other item to the same
        # section under its existing title, creating a genuine target collision.
        from test_importer import append_row
        append_row(f.export,'Proposed Renames',{'Rename ID':'R-COLLISION','Source ID':'collision',
            'Current name + type suffix':'Approved.pdf','Proposed name + type suffix':'Approved.pdf',
            'Proposed destination folder':fixtures.SECTION})
        with self.assertRaisesRegex(Exception,'DESTINATION_EXACT_NAME_COLLISION'):
            f.run_command()
        self.assertEqual(f.server.requests,[])

    def test_apply_journals_actual_preimage_and_rollback_restores_it(self):
        f=self.fixture();f.server.drive.files['leaf']['name']='Actual starting.pdf'
        original_export=copy.deepcopy(f.export)
        result=f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2})
        j=Journal(f.config['store']);events=j.records()
        warning=next(e for e in events if e['kind']=='SOURCE_NAME_DRIFT_ACCEPTED')
        p=warning['payload']
        self.assertEqual((p['original_name'],p['observed_name'],p['approved_target_name']),
            ('Old.pdf','Actual starting.pdf','Approved.pdf'))
        attempted=next(e for e in events if e['kind']=='DESIRED_ATTEMPTED' and e['payload']['source_id']=='leaf')
        self.assertLess(warning['seq'],attempted['seq'])
        self.assertEqual(j.read_evidence(attempted['payload']['before_ref'])['name'],'Actual starting.pdf')
        self.assertEqual(j.read_evidence(p['observed_ref'])['name'],'Actual starting.pdf')
        self.assertEqual(f.export,original_export)
        rollback=f.run_command(apply=True,rollback=result['run_id'])
        self.assertEqual(rollback['counts'],{'ROLLED_BACK':2})
        self.assertEqual(f.server.drive.files['leaf']['name'],'Actual starting.pdf')
        self.assertEqual(f.server.drive.files['leaf']['parents'],['source-root'])

    def test_change_after_freeze_is_still_blocked_before_child_write(self):
        f=self.fixture();f.config['workers']=1
        f.server.drive.files['leaf']['name']='Actual starting.pdf'
        def change_child(args,response):
            if args['fileId']=='source-root':f.server.drive.files['leaf']['name']='Later human edit.pdf'
        f.server.after=change_child
        result=f.run_command(apply=True)
        self.assertEqual([r['fileId'] for r in f.server.requests],['source-root'])
        self.assertEqual(f.server.drive.files['leaf']['name'],'Later human edit.pdf')
        self.assertEqual(result['counts'].get('APPLIED'),1)

    def test_warning_is_readable_in_console(self):
        output=io.StringIO();reporter=ConsoleReporter(stream=output)
        reporter.progress({'stage':'starting_name_warning','source_id':'file',
            'observed_name':'Manual.pdf','approved_target_name':'Approved.pdf'})
        self.assertIn('Warning: starting name differs',output.getvalue())
        self.assertIn('Manual.pdf → Approved.pdf',output.getvalue())
        self.assertIn('rollback',output.getvalue())


if __name__=='__main__':unittest.main()
