"""Offline regressions for read-only parents whose own ancestry is unavailable."""
import copy
import unittest

import test_destination_command as destination_fixture
import test_destination_hybrid_importer as hybrid_fixture
from test_desired import FakeDrive, ProcessDeath
from uqmc_recovery.agent_profile import agent_parent_context
from uqmc_recovery.desired import DesiredRunner
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import SafetyError
from uqmc_recovery.section_execution import collect_metadata, normalize


class ParentContextTests(unittest.TestCase):
    def context_raw(self):
        value = destination_fixture.raw('boundary', 'Project boundary', [], True)
        del value['parents']
        return value

    def fixture(self):
        fixture = destination_fixture.DestinationCommandTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for sid in ('old-parent', 'club'):
            fixture.server.drive.files[sid].pop('parents')
        return fixture

    def assert_unknown_ancestry(self, value):
        self.assertNotEqual(value.get('parents'), [], 'Unknown ancestry must not become a root claim')
        self.assertIsNone(value.get('parents'))
        self.assertIsNot(value.get('parents_complete'), True)

    def assert_parent_evidence_preserved(self, fixture):
        journal = Journal(fixture.config['store'])
        seen = set()
        for event in journal.records():
            if event['kind'] != 'DESIRED_PREPARED':
                continue
            parents = journal.read_evidence(event['payload']['parents_ref'])
            for sid in ('old-parent', 'club'):
                if sid in parents:
                    seen.add(sid)
                    self.assert_unknown_ancestry(parents[sid])
        self.assertEqual(seen, {'old-parent', 'club'})

    def test_unknown_ancestry_is_context_only_never_an_empty_parent_list(self):
        value = self.context_raw()
        original = copy.deepcopy(value)
        context = normalize(value, [], parent_context=True)
        self.assert_unknown_ancestry(context)
        self.assertEqual(context['subtree']['members'], [])
        self.assertEqual(value, original)
        with self.assertRaises(SafetyError):
            normalize(value, [])

    def test_context_requires_complete_listing_when_requested(self):
        context = normalize(self.context_raw(), [], parent_context=True)
        context.pop('subtree')
        with self.assertRaises(SafetyError):
            agent_parent_context(context)
        actual = agent_parent_context(context, require_subtree=False)
        self.assert_unknown_ancestry(actual)

    def test_context_keeps_identity_folder_and_supplied_parent_validation(self):
        valid = normalize(self.context_raw(), [], parent_context=True)
        for change in ({'id': ''}, {'name': ''}, {'mime_type': 'application/pdf'},
                       {'trashed': True}, {'parents': 'unknown'},
                       {'parents': ['boundary'], 'parents_complete': True},
                       {'capabilities': []}):
            with self.subTest(change=change), self.assertRaises(SafetyError):
                agent_parent_context({**valid, **change})

    def test_fake_destination_freeze_apply_and_rollback_preserve_unknown_ancestry(self):
        fixture = self.fixture()
        result = fixture.run_command(apply=True)
        self.assertEqual(result['counts'], {'APPLIED': 2}, result)
        self.assertEqual(fixture.server.requests[0], {
            'fileId': 'source-root', 'name': 'Skills',
            'addParents': 'club', 'removeParents': 'old-parent'})
        self.assertEqual(fixture.server.created, [])
        self.assertEqual(fixture.server.drive.files['source-root']['parents'], ['club'])
        self.assert_parent_evidence_preserved(fixture)
        restored = fixture.run_command(apply=True, rollback=result['run_id'])
        self.assertEqual(restored['counts'], {'ROLLED_BACK': 2}, restored)
        self.assertEqual(fixture.server.drive.files['source-root']['name'], 'Old folder')
        self.assertEqual(fixture.server.drive.files['source-root']['parents'], ['old-parent'])
        self.assertEqual(fixture.server.drive.files['leaf']['name'], 'Old.pdf')
        self.assert_parent_evidence_preserved(fixture)
        for sid in ('club', 'old-parent'):
            self.assertNotIn('parents', fixture.server.drive.files[sid])
            self.assertFalse(any(request['fileId'] == sid for request in fixture.server.requests))

    def test_interrupted_folder_success_resumes_without_replaying_it(self):
        fixture = self.fixture()
        fixture.config['workers'] = 1
        def stop_after_folder(args, result):
            if args['fileId'] == 'source-root':
                raise ProcessDeath()
        fixture.server.after = stop_after_folder
        with self.assertRaises(ProcessDeath):
            fixture.run_command(apply=True)
        self.assertEqual(fixture.server.drive.files['source-root']['parents'], ['club'])
        fixture.server.after = None
        result = fixture.run_command(apply=True, resume=True)
        self.assertEqual(result['counts'], {'APPLIED': 2}, result)
        self.assertEqual([request['fileId'] for request in fixture.server.requests], ['source-root', 'leaf'])
        self.assert_parent_evidence_preserved(fixture)

    def test_mixed_source_still_retains_unrelated_member_and_container(self):
        fixture = self.fixture()
        fixture.mixed()
        source = copy.deepcopy(fixture.server.drive.files['source-root'])
        unrelated = copy.deepcopy(fixture.server.drive.files['eoi'])
        result = fixture.run_command(apply=True)
        self.assertEqual(result['counts'], {'APPLIED': 1}, result)
        self.assertEqual(fixture.server.drive.files['source-root'], source)
        self.assertEqual(fixture.server.drive.files['eoi'], unrelated)
        self.assertNotIn(('get', 'eoi'), fixture.server.reads)
        self.assertEqual([request['fileId'] for request in fixture.server.requests], ['leaf'])

    def test_non_anchor_destination_cannot_claim_unknown_placement(self):
        fixture = hybrid_fixture.HybridImporterTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        prepared = fixture.prepare()
        state = fixture.states['skills']
        state.pop('parents')
        state['parents_complete'] = False
        # The projection's exact placement proof cannot be weakened by treating
        # an actual destination path like the unrelated original-parent boundary.
        result = fixture.run_import(prepared)
        self.assertIsNone(result.plan, result.manifest)
        self.assertTrue(result.manifest.get('section_reason_code'), result.manifest)

    def test_collection_rejects_unknown_own_parents_on_selected_source(self):
        fixture = self.fixture()
        fixture.server.drive.files['source-root'].pop('parents')
        def factory(allowed):
            return fixture.server.factory(allowed, set(), set(), False)
        with self.assertRaisesRegex(SafetyError, 'AGENT_COMPLETE_PARENT_IDS_REQUIRED'):
            collect_metadata({'source-root', 'leaf'}, factory, workers=1)
        self.assertEqual(fixture.server.requests, [])
        self.assertFalse(any(kind == 'list' for kind, sid in fixture.server.reads))

    def test_collection_requires_selected_child_in_exact_parent_listing(self):
        for change in ('omitted', 'name', 'mimeType', 'parents'):
            with self.subTest(change=change):
                fixture = self.fixture()
                def factory(allowed):
                    client = fixture.server.factory(allowed, set(), set(), False)
                    original = client.list_children
                    def list_children(sid):
                        children = original(sid)
                        if sid == 'old-parent':
                            if change == 'omitted':
                                return []
                            child = next(v for v in children if v['id'] == 'source-root')
                            child[change] = ['another-parent'] if change == 'parents' else 'different'
                        return children
                    client.list_children = list_children
                    return client
                with self.assertRaisesRegex(SafetyError,
                        'SOURCE_PARENT_MEMBERSHIP_CONFLICT|INVALID_SECTION_READ_CACHE'):
                    collect_metadata({'source-root', 'leaf'}, factory, workers=1)
                self.assertEqual(fixture.server.requests, [])

    def test_actual_runner_source_still_requires_complete_parent_ids(self):
        runner = DesiredRunner.__new__(DesiredRunner)
        runner.transport = FakeDrive({'boundary': self.context_raw()})
        runner.max_attempts = 1
        runner.audit_permissions = False
        with self.assertRaisesRegex(SafetyError, 'INCOMPLETE_CURRENT_STATE'):
            runner.read('boundary')
        context = runner.read_parent_context('boundary')
        self.assert_unknown_ancestry(context)
        self.assertEqual(runner.transport.calls, [])


if __name__ == '__main__':
    unittest.main()
