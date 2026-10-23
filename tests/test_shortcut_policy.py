"""Offline shortcut scope policy, including old approved plans and history."""
import copy
import tempfile
import unittest
from unittest.mock import Mock

import test_desired as fixtures
import test_destination_subtree_coverage as coverage_fixtures
from uqmc_recovery.action_bridge import ActionBridge
from uqmc_recovery.destination_publication import freeze_publication
from uqmc_recovery.importer import HEADERS, catalog
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, Operation, SafetyError
from uqmc_recovery.shortcut_policy import (SHORTCUT_MIME, REASON, active_scope_status,
    apply_shortcut_policy, excluded_shortcut_ids, guard_shortcut_mutation,
    persist_shortcut_exclusions)


class ShortcutPolicyTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.DesiredStateTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_catalog_uses_mime_not_filename_and_does_not_make_private_exclusion(self):
        fixture = coverage_fixtures.WholeSubtreeCoverageTests(methodName='runTest')
        fixture.setUp()
        headers = HEADERS['Rename Coverage']
        rows = fixture.export['sheets']['Rename Coverage'][1:]
        sid = rows[0][headers.index('Source ID')]
        rows[0][headers.index('Verified MIME type')] = SHORTCUT_MIME
        for key in ('Current name + type suffix', 'Proposed name / retained name'):
            rows[1][headers.index(key)] = 'Ordinary.shortcut'
        rows[1][headers.index('Existing proposed target')] = coverage_fixtures.DESTINATION + '/Ordinary.shortcut'
        source = catalog(fixture.export, include_subtree_coverage=True)
        self.assertIn(sid, source['excluded_ids'])
        self.assertNotIn(sid, source['private_excluded_ids'])
        self.assertEqual(source['shortcut_ids'], [sid])
        self.assertFalse(any(r['source_id'] == sid for r in source['records'].values()))
        self.assertNotIn(rows[1][headers.index('Source ID')], source['excluded_ids'])

    def test_exclusion_is_durable_idempotent_and_does_not_resolve_uncertainty(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            event = journal.append('DESIRED_STOPPED', payload={'source_id':'link', 'state':'CONFLICT',
                'reason_code':'PROTECTED_METADATA_DRIFT'})
            source = {'records':{}, 'input_sha256':'fixture-hash'}
            apply_shortcut_policy(source, {'link':{'id':'link','mimeType':SHORTCUT_MIME}})
            persist_shortcut_exclusions(journal,source)
            persist_shortcut_exclusions(journal,source)
            self.assertEqual(len(journal.records()),2)
            self.assertEqual(journal.records()[0], event)
            self.assertEqual(excluded_shortcut_ids(journal.records()),{'link'})
            fresh = {'excluded_ids':[], 'records':{}}
            apply_shortcut_policy(fresh,records=journal.records())
            self.assertEqual(fresh['excluded_ids'],['link'])
            self.assertFalse(any(r['kind']=='DESIRED_APPLIED' for r in journal.records()))

    def test_old_frozen_shortcut_plan_is_skipped_without_replaying_history(self):
        fixture = self.fixture()
        fixture.files['fixture-source']['mimeType'] = SHORTCUT_MIME
        before = fixture.snapshot('fixture-source')
        op = Operation('shortcut:move','fixture-source','MOVE',before,{'parents':['fixture-destination']})
        runner = fixture.freeze((op,))
        fixture.historical(op, 'UNCERTAIN')
        saved = copy.deepcopy(fixture.journal.records())
        runner = fixture.runner()
        result = runner.preview()
        self.assertEqual(result['total_plan_items'],0)
        self.assertEqual(result['scope_dispositions'][0]['reason_code'],REASON)
        self.assertEqual(fixture.transport.calls,[])
        self.assertEqual(fixture.journal.records(),saved)

    def test_live_shortcut_preflight_and_legacy_bridge_reject_before_dispatch(self):
        fixture = self.fixture()
        runner = fixture.freeze()
        item = runner.items[0]
        current = copy.deepcopy(fixture.transport.files['fixture-source'])
        current['mimeType'] = SHORTCUT_MIME
        with self.assertRaisesRegex(SafetyError, REASON):
            runner.preflight(item,current,item['target'])
        bridge = object.__new__(ActionBridge)
        op = copy.deepcopy(fixture.plan.operations[0])
        bridge._frozen = Mock(return_value=(op,'fixture'))
        bridge._agent_authority = Mock(side_effect=AssertionError('must reject first'))
        with self.assertRaisesRegex(SafetyError, REASON):
            bridge._validate_agent(fixture.plan,op.operation_id,None,current,{})
        bridge._agent_authority.assert_not_called()
        self.assertEqual(fixture.transport.calls,[])

    def test_indirect_folder_move_blocked_but_rename_and_ordinary_child_allowed(self):
        before = {'mime_type':FOLDER,'parents':['old'],'subtree':{'members':[
            {'id':'link','mime_type':SHORTCUT_MIME}]}}
        with self.assertRaisesRegex(SafetyError,'SHORTCUT_DESCENDANT_MUST_STAY_IN_PLACE'):
            guard_shortcut_mutation(before, {'parents':['new']})
        guard_shortcut_mutation(before, {'name':'Renamed folder'})
        before['subtree']['members'][0]['mime_type']='application/pdf'
        guard_shortcut_mutation(before, {'parents':['new']})

    def test_live_mime_exclusion_captures_evidence_without_changing_proposal_rows(self):
        source = {'records':{'r':{'source_id':'link','action':'MOVE'}}, 'excluded_ids':[]}
        original = copy.deepcopy(source['records'])
        observed = {'id':'link','mimeType':SHORTCUT_MIME,'name':'Link','parents':['old']}
        apply_shortcut_policy(source, {'link':observed})
        self.assertEqual(source['records'],original)
        self.assertEqual(source['shortcut_evidence']['link'],observed)
        self.assertEqual(source['excluded_ids'],['link'])

    def test_folder_live_read_blocks_carrying_shortcut_on_forward_and_rollback(self):
        fixture = self.fixture()
        runner = fixture.freeze()
        runner._folder_graph = Mock(return_value=({}, {}))
        runner.children = Mock(return_value=[{'id':'link','name':'Link','parents':['folder'],
                                             'mimeType':SHORTCUT_MIME}])
        item = {'source_id':'folder','original':{'parents':['old']},
                'baseline':{'subtree':{'complete':True,'members':[{'id':'link'}]}}}
        for parent in ['new','old']:
            with self.subTest(parent=parent), self.assertRaisesRegex(SafetyError,'SHORTCUT_DESCENDANT_MUST_STAY_IN_PLACE'):
                runner._check_folder_members(item, {'parents':[parent]})
        self.assertEqual(fixture.transport.calls,[])

    def test_active_counts_remove_both_completed_and_conflicted_shortcuts(self):
        result = active_scope_status(['file','applied-link','conflicted-link'],
            ['file','applied-link'], ['applied-link','conflicted-link'])
        self.assertEqual((result['active_count'],result['verified_active_count']),(1,1))
        self.assertEqual(result['remaining_active_ids'],[])
        self.assertEqual(len(result['excluded_ids']),2)
        self.assertTrue(result['historical_effects_preserved'])

    def test_unsupported_public_disposition_stops_before_network_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            persist_shortcut_exclusions(journal,{'shortcut_ids':['link'],'records':{}})
            drive = Mock()
            with self.assertRaisesRegex(ValueError,'SHORTCUT_POLICY_PUBLICATION_REQUIRES_SUPPORTED_DISPOSITION'):
                freeze_publication({}, {}, journal=journal, fresh_workbook={}, drive=drive)
            self.assertEqual(drive.mock_calls,[])
