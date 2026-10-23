"""Frozen-batch execution and recovery never adopt live spreadsheet edits."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from test_destination_command import DestinationCommandTests, SECTION
from test_desired import ProcessDeath
from uqmc_recovery.importer import HEADERS
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import SafetyError


class FrozenBatchTests(unittest.TestCase):
    def setUp(self):
        self.f=DestinationCommandTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.config['workers']=1

    def interrupt(self):
        def die(args,result):
            if args['fileId']=='source-root':raise ProcessDeath()
        self.f.server.after=die
        with self.assertRaises(ProcessDeath):self.f.run_command(apply=True)
        self.f.server.after=None
        return json.loads(Path(self.f.config['registry']).read_text())['runs'][-1]

    def unavailable_sheet(self):
        self.f.capture.capture=Mock(side_effect=OSError('sheet unavailable'))
        self.f.capture._revision=Mock(side_effect=AssertionError('unexpected live revision read'))

    def change_sheet(self):
        row=self.f.export['sheets']['Proposed Renames'][2]
        row[HEADERS['Proposed Renames'].index('Proposed name + type suffix')]='Later proposal.pdf'
        row[HEADERS['Proposed Renames'].index('Decision (Yes/No)')]='No'

    def test_resume_uses_original_snapshot_despite_new_target_and_live_sheet_outage(self):
        saved=self.interrupt();plan=Path(saved['plan_path']).read_bytes()
        self.change_sheet();self.unavailable_sheet()
        result=self.f.run_command(apply=True,resume=True)
        self.assertEqual(result['counts'],{'APPLIED':2})
        self.assertEqual([a['fileId'] for a in self.f.server.requests],['source-root','leaf'])
        self.assertEqual(self.f.server.drive.files['leaf']['name'],'Approved.pdf')
        self.assertEqual(Path(saved['plan_path']).read_bytes(),plan)
        self.f.capture.capture.assert_not_called();self.f.capture._revision.assert_not_called()
        self.assertEqual(result['plan_consistency'],'FROZEN_APPROVED_SNAPSHOT')

    def test_sheet_changes_during_execution_do_not_change_remaining_instructions(self):
        def change_after_dispatch(args,result):
            if args['fileId']=='source-root':
                self.change_sheet();self.unavailable_sheet()
        self.f.server.after=change_after_dispatch
        result=self.f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2})
        self.assertEqual(self.f.server.drive.files['leaf']['name'],'Approved.pdf')
        self.f.capture._revision.assert_not_called()
        self.assertIsNone(result['revision_stop_reason'])

    def test_completed_resume_does_not_replay_or_read_new_plan(self):
        self.f.run_command(apply=True);requests=copy.deepcopy(self.f.server.requests)
        self.change_sheet();self.unavailable_sheet()
        result=self.f.run_command(apply=True,resume=True)
        self.assertEqual(result['counts'],{})
        self.assertEqual(result['skipped_completed_items'],2)
        self.assertEqual(self.f.server.requests,requests)
        self.f.capture.capture.assert_not_called()

    def test_publication_refresh_failure_cannot_cancel_or_replay_execution(self):
        self.interrupt();self.unavailable_sheet()
        result=self.f.run_command(apply=True,resume=True,publisher=None)
        self.assertEqual(result['counts'],{'APPLIED':2})
        self.assertEqual(result['publication']['state'],'PENDING')
        self.f.capture.capture.assert_called_once()  # Publication only, after saving results.
        self.f.capture._revision.assert_not_called()
        self.assertEqual([a['fileId'] for a in self.f.server.requests],['source-root','leaf'])

    def test_new_batch_still_requires_fresh_sheet(self):
        self.unavailable_sheet()
        with self.assertRaisesRegex(OSError,'sheet unavailable'):
            self.f.run_command(apply=True)
        self.assertEqual(self.f.server.requests,[])
        self.assertEqual(self.f.server.created,[])

    def test_explicit_local_revocation_still_stops_saved_batch(self):
        self.interrupt();requests=copy.deepcopy(self.f.server.requests)
        j=Journal(self.f.config['store'])
        origin=next(e for e in reversed(j.records()) if e['kind']=='SECTION_IMPORTED')
        owner=j.read_evidence(origin['payload']['owner_approval_ref'])
        j.append('SECTION_APPROVAL_REVOKED',payload={'approval_id':owner['approval_id']})
        self.unavailable_sheet()
        with self.assertRaisesRegex(SafetyError,'REVOKED'):
            self.f.run_command(apply=True,resume=True)
        self.assertEqual(self.f.server.requests,requests)
        self.f.capture.capture.assert_not_called()

    def test_changed_saved_plan_hash_remains_blocked(self):
        saved=self.interrupt();path=Path(saved['plan_path']);value=json.loads(path.read_text())
        value['operations'][-1]['target']['name']='Injected.pdf';path.write_text(json.dumps(value))
        self.unavailable_sheet();requests=copy.deepcopy(self.f.server.requests)
        with self.assertRaises(SafetyError):self.f.run_command(apply=True,resume=True)
        self.assertEqual(self.f.server.requests,requests)
