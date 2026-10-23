"""Focused unfinished-only resume and append-only history regression tests."""
import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
import test_desired as fixtures
from uqmc_recovery.desired import core


class ResumeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DesiredStateTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.add_file()
        self.f.freeze((*self.f.operations(),*self.f.operations('fixture-other',target_name='Other approved.pdf')))
        self.runner=self.f.runner(audit_permissions=False)

    def run_plan(self, **kw):
        return self.runner.run(apply=True,confirm_plan_sha256=self.f.plan.proposal_hash,workers=1,**kw)

    def first_done(self):
        self.assertEqual(self.run_plan(limit=1)['counts'],{'APPLIED':1})
        return self.runner.by_id['fixture-source']

    def test_resume_skips_completed_reads_and_processes_only_unfinished(self):
        self.first_done();reads=copy.deepcopy(self.f.transport.reads)
        result=self.run_plan(resume=True)
        self.assertEqual(result['counts'],{'APPLIED':1})
        self.assertEqual((result['selected_items'],result['skipped_completed_items']),(1,1))
        self.assertEqual(self.f.transport.reads['fixture-source'],reads['fixture-source'])
        self.assertEqual([c['fileId'] for c in self.f.transport.calls],['fixture-source','fixture-other'])

    def test_later_content_metadata_observation_is_retained_not_replayed(self):
        item=self.first_done();active=self.runner.latest_change(item)
        old=self.f.journal.read_evidence(active['payload']['applied_ref'])
        changed={**old,'size':'100','version':'9','modifiedTime':'2026-03-01T00:00:00Z'}
        self.runner._record_comparison(item,old,changed,stage='RECORDED_APPLIED',outcome='CONFLICT',reason='PROTECTED_METADATA_DRIFT')
        self.runner.record('STOPPED',item,state='CONFLICT',reason='PROTECTED_METADATA_DRIFT')
        before=self.f.journal.path.read_bytes()
        with self.f.journal.locked():pending,done,warnings=self.runner.resume_partition(self.runner.items)
        self.assertEqual(done,['fixture-source']);self.assertEqual([i['source_id'] for i in pending],['fixture-other'])
        self.assertEqual(warnings[0]['reason'],'POST_COMPLETION_METADATA_CHANGE_RETAINED')
        self.assertEqual(self.f.journal.path.read_bytes(),before)
        self.assertEqual(self.f.journal.read_evidence(active['payload']['applied_ref']),old)

    def test_genuine_location_conflict_and_uncertain_request_are_not_skipped(self):
        item=self.first_done();old=self.f.journal.read_evidence(self.runner.latest_change(item)['payload']['applied_ref'])
        self.runner._record_comparison(item,old,{**old,'parents':['external']},stage='RECORDED_APPLIED',outcome='CONFLICT',reason='CONCURRENT_CHANGE')
        with self.f.journal.locked():pending,done,_=self.runner.resume_partition(self.runner.items)
        self.assertEqual(done,[]);self.assertEqual(len(pending),2)
        self.runner.record('ATTEMPTED',item,direction='FORWARD',before_ref=self.f.journal.evidence(old),target_ref=self.f.journal.evidence(item['target']))
        with self.f.journal.locked():self.assertEqual(self.runner.resume_partition(self.runner.items)[1],[])

    def test_partial_success_stays_pending_and_rollback_history_not_skipped(self):
        item=self.runner.by_id['fixture-source']
        with self.f.journal.shared_locked(),self.runner.authority_session():
            raw,_,_=self.runner.observe(item)
            self.runner.transition(item,raw,core(raw)|{'name':item['target']['name']})
            self.assertEqual(self.runner.resume_partition(self.runner.items)[1],[])
        self.run_plan(resume=True)
        self.run_plan(rollback=True)
        with self.f.journal.locked():self.assertEqual(self.runner.resume_partition(self.runner.items)[1],[])

    def test_index_uses_incremental_tails_and_is_defensive_and_thread_safe(self):
        j=self.f.journal
        with j.shared_locked():
            self.runner.events()
            with patch.object(j,'records',side_effect=AssertionError('full history copied')):
                def add(n):
                    self.runner.record('TEST',self.runner.items[0],number=n)
                    values=self.runner.events('fixture-source')
                    values[-1]['payload']['number']='changed outside index'
                with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(add,range(12)))
                values=self.runner.events('fixture-source')
                self.assertEqual(sorted(e['payload']['number'] for e in values if e['kind']=='DESIRED_TEST'),list(range(12)))
        # Reacquire and validate a new lock lifetime; include newly appended data.
        self.runner.record('TEST',self.runner.items[0],number=12)
        self.assertEqual(len([e for e in self.runner.events('fixture-source') if e['kind']=='DESIRED_TEST']),13)
