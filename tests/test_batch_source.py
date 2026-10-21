"""Offline handoff checks; no live workbook or file calls."""
import copy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
import test_batch_refresh as fixtures
from uqmc_recovery.batch_source import bind_batch_source, require_batch_source
from uqmc_recovery.model import Plan, Operation, SafetyError

class BatchSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.BatchRefreshTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.fresh['exported_at_utc']=datetime.now(timezone.utc).isoformat()
        self.journal=self.fixture.journal
        self.result=self.fixture.refresh('new-batch')
        self.plan=Plan('new-batch','new-batch','refresh-fixture',self.fixture.plan.operations)
    def bind(self,plan=None):
        # Origin/metadata registration has its own existing fixture tests.
        with patch('uqmc_recovery.agent_profile.register_agent_plan'):
            return bind_batch_source(self.journal,plan or self.plan,self.result['evidence_ref'])
    def test_new_apply_requires_refresh_binding_then_exact_target_passes(self):
        with self.assertRaisesRegex(SafetyError,'REQUIRES_FRESH_WORKBOOK'):
            require_batch_source(self.journal,self.plan)
        self.bind()
        require_batch_source(self.journal,self.plan)
        self.assertFalse(any(e['kind']=='APPROVED' for e in self.journal.records()))
    def test_wrong_name_destination_or_source_cannot_be_bound(self):
        for field,value in [('name','Unapproved.pdf'),('parents',['outside'])]:
            operations=list(self.plan.operations)
            old=operations[0 if field=='name' else 1]
            operations[0 if field=='name' else 1]=Operation(old.operation_id,old.file_id,old.action,
                old.before,{field:value},old.depends_on)
            changed=Plan('new-batch','new-batch','changed',tuple(operations))
            with self.subTest(field=field),self.assertRaises(SafetyError): self.bind(changed)
    def test_old_snapshot_cannot_start_new_apply(self):
        with patch('uqmc_recovery.batch_source.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime.now(timezone.utc)+timedelta(minutes=6)
            with self.assertRaisesRegex(SafetyError,'EXPIRED_REFRESH_AGAIN'): self.bind()
    def test_unstarted_resume_cannot_bypass_refresh(self):
        with self.assertRaisesRegex(SafetyError,'REQUIRES_FRESH_WORKBOOK'):
            require_batch_source(self.journal,self.plan,resume=True)
        self.journal.append('DESIRED_ATTEMPTED',payload={'plan_sha256':self.plan.proposal_hash})
        require_batch_source(self.journal,self.plan,resume=True)
    def test_edit_notice_blocks_started_resume(self):
        self.journal.append('DESIRED_ATTEMPTED',payload={'plan_sha256':self.plan.proposal_hash})
        self.journal.append('BATCH_REFRESH_REQUIRED',payload={'proposal_hash':self.plan.proposal_hash})
        with self.assertRaisesRegex(SafetyError,'PLAN_EDITS_REQUIRE_NEW_REFRESHED_BATCH'):
            require_batch_source(self.journal,self.plan,resume=True)
    def test_new_observation_after_freeze_requires_reconciliation(self):
        self.fixture.observe()
        with self.assertRaisesRegex(SafetyError,'HISTORY_CHANGED_SINCE_BATCH_REFRESH'): self.bind()
    def test_pending_rename_edit_is_exactly_bound_for_executor(self):
        from test_importer import set_cell
        set_cell(self.fixture.fresh,'Proposed Renames',1,'Proposed name + type suffix','Updated.pdf')
        self.result=self.fixture.refresh('edited-batch')
        before=self.fixture.plan.operations[0].before
        rename=Operation('new-rename','source','RENAME',before,{'name':'Updated.pdf'})
        move=Operation('new-move','source','MOVE',rename.intended(),{'parents':['new']},('new-rename',))
        self.plan=Plan('edited-batch','edited-batch','refresh-edited',(rename,move))
        self.bind()
        require_batch_source(self.journal,self.plan)

if __name__=='__main__': unittest.main()
