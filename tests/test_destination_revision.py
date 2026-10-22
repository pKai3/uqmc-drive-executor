"""Explicit revision fake-Drive tests; all state and remote requests are synthetic."""
import copy
import json
import unittest
from pathlib import Path

import test_destination_command as command_fixtures
from test_destination_command import SECTION, raw
from test_desired import ProcessDeath
from uqmc_recovery.destination import run_destination
from uqmc_recovery.destination_history import build_history_index
from uqmc_recovery.destination_revision import SCHEMA, read_manifest
from uqmc_recovery.importer import HEADERS, WORKBOOK_ID, catalog
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import SafetyError, digest


class DestinationRevisionTests(unittest.TestCase):
    def setUp(self):
        f = self.f = command_fixtures.DestinationCommandTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.mixed()
        self.old_path = SECTION + '/Category/2020'
        self.new_path = SECTION + '/2020/Category'
        self.set_cell('Move Manifest', 1, 'Exact target path', self.old_path)
        self.set_cell('Proposed Moves', 1, 'Proposed target path', self.old_path)
        self.set_cell('Proposed Renames', 1, 'Proposed name + type suffix', '2020')
        self.set_cell('Proposed Renames', 1, 'Proposed destination folder', SECTION + '/Category')
        self.set_cell('Proposed Renames', 2, 'Proposed destination folder', self.old_path)
        first = f.run_command(apply=True)
        self.assertEqual(first['counts'], {'APPLIED': 1})
        self.first_hash = json.loads(Path(f.config['registry']).read_text())['runs'][-1]['plan_sha256']
        self.folder = f.publications[-1]['sections'][0]['destination_paths'][self.old_path]
        self.before = copy.deepcopy(f.server.drive.files[self.folder])
        self.first_requests = copy.deepcopy(f.server.requests)
        self.set_cell('Move Manifest', 1, 'Exact target path', self.new_path)
        self.set_cell('Proposed Moves', 1, 'Proposed target path', self.new_path)
        self.set_cell('Proposed Renames', 1, 'Proposed name + type suffix', 'Category')
        self.set_cell('Proposed Renames', 1, 'Proposed destination folder', SECTION + '/2020')
        self.set_cell('Proposed Renames', 2, 'Proposed destination folder', self.new_path)
        self.manifest = self.make_manifest()

    def set_cell(self, tab, row, key, value):
        self.f.export['sheets'][tab][row][HEADERS[tab].index(key)] = value

    def make_manifest(self):
        source = catalog(self.f.capture.capture(), expected_workbook_id=WORKBOOK_ID,
                         include_subtree_coverage=True, subtree_section_path=SECTION)
        return {'schema_version': SCHEMA, 'revision_id': 'year-first-fixture', 'section_path': SECTION,
                'workbook_id': WORKBOOK_ID, 'input_sha256': source['input_sha256'],
                'items': [{'source_id': self.folder, 'before_path': self.old_path,
                           'target_path': self.new_path, 'target_name': 'Category'}],
                'new_folders': [{'path': SECTION + '/2020', 'name': '2020'}]}

    def run_revision(self, **kwargs):
        return self.f.run_command(revision_manifest=self.manifest, **kwargs)

    def test_preview_and_apply_move_created_folder_intact_without_replaying_child(self):
        before = copy.deepcopy(self.f.server.drive.files)
        preview = self.run_revision()
        self.assertEqual(preview['counts'], {'READY': 1}, preview)
        self.assertEqual(self.f.server.drive.files, before)
        result = self.run_revision(apply=True)
        self.assertEqual(result['counts'], {'APPLIED': 1}, result)
        requests = self.f.server.requests[len(self.first_requests):]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['fileId'], self.folder)
        self.assertEqual(requests[0]['name'], 'Category')
        self.assertIn('addParents', requests[0])
        self.assertEqual(self.f.server.drive.files['leaf']['parents'], [self.folder])
        registry = json.loads(Path(self.f.config['registry']).read_text())
        run = registry['runs'][-1]
        self.assertNotEqual(run['plan_sha256'], self.first_hash)
        evidence = Journal(self.f.config['store']).read_evidence(run['revision_origin_ref'])
        self.assertEqual(evidence['carried_descendants'][0]['source_id'], 'leaf')
        self.assertTrue(evidence['carried_descendants'][0]['prior_observation_ref'])
        self.assertEqual(evidence['plan']['operations'][0]['before']['name'], self.before['name'])
        self.assertEqual(evidence['prior_completion_proofs'][self.folder]['kind'], 'VERIFIED_CREATION')
        h = build_history_index(Journal(self.f.config['store']))
        self.assertEqual(h['leaf']['plan_hashes'], [self.first_hash])
        self.assertEqual(registry['sections'][0]['publication_spec']['source_ids'], ['leaf'])

    def test_ordinary_command_after_revision_skips_carried_completed_work(self):
        self.run_revision(apply=True); requests = copy.deepcopy(self.f.server.requests)
        result = self.f.run_command(apply=True)
        self.assertTrue(result['no_op'], result)
        self.assertEqual(self.f.server.requests, requests)

    def test_explicit_revision_repeat_is_noop(self):
        self.run_revision(apply=True); requests = copy.deepcopy(self.f.server.requests)
        result = self.run_revision(apply=True)
        self.assertTrue(result['no_op'], result)
        self.assertEqual(self.f.server.requests, requests)

    def test_response_lost_after_success_resumes_original_revision_without_replay(self):
        self.f.config['workers'] = 1
        def die(args, result):
            if args['fileId'] == self.folder:raise ProcessDeath()
        self.f.server.after = die
        with self.assertRaises(ProcessDeath):self.run_revision(apply=True)
        self.f.server.after = None
        with self.assertRaisesRegex(SafetyError, 'REVISION_UNRESOLVED_PRIOR_REQUEST'):
            self.run_revision()
        count = len(self.f.server.requests)
        result = self.run_revision(apply=True, resume=True)
        self.assertEqual(result['counts'], {'APPLIED': 1}, result)
        self.assertEqual(len(self.f.server.requests), count)

    def test_rollback_restores_this_revision_preimage_not_original_source(self):
        result = self.run_revision(apply=True)
        rollback = self.f.run_command(apply=True, rollback=result['run_id'])
        self.assertEqual(rollback['counts'], {'ROLLED_BACK': 1}, rollback)
        current = self.f.server.drive.files[self.folder]
        self.assertEqual(current['name'], self.before['name'])
        self.assertEqual(current['parents'], self.before['parents'])
        self.assertEqual(self.f.server.drive.files['leaf']['name'], 'Approved.pdf')
        self.assertEqual(self.f.server.drive.files['leaf']['parents'], [self.folder])

    def test_stale_manifest_and_unplanned_child_block_before_creation(self):
        self.manifest['input_sha256'] = 'stale'
        with self.assertRaisesRegex(SafetyError, 'FRESH_SHEET_BINDING'):
            self.run_revision(apply=True)
        self.manifest = self.make_manifest()
        self.f.server.drive.files['intruder'] = raw('intruder', 'Unplanned.pdf', [self.folder])
        count = len(self.f.server.created)
        with self.assertRaisesRegex(SafetyError, 'DESCENDANT_MISSING_FRESH_PROPOSAL'):
            self.run_revision(apply=True)
        self.assertEqual(len(self.f.server.created), count)
        self.assertEqual(self.f.server.requests, self.first_requests)

    def test_external_completed_name_drift_and_collision_remain_blocked(self):
        self.f.server.drive.files[self.folder]['name'] = 'Manual edit'
        result = self.run_revision(apply=True)
        self.assertEqual(result['counts'], {'BLOCKED': 1}, result)
        self.assertEqual(result['blockers'][0]['reason'], 'REVISION_COMPLETED_PREIMAGE_DRIFT')
        self.assertEqual(self.f.server.requests, self.first_requests)

    def test_changed_child_target_requires_separate_explicit_revision(self):
        self.set_cell('Proposed Renames', 2, 'Proposed name + type suffix', 'Another.pdf')
        self.manifest = self.make_manifest()
        with self.assertRaisesRegex(SafetyError, 'DESCENDANT_REQUIRES_SEPARATE_WORK'):
            self.run_revision(apply=True)

    def test_creation_interruption_reuses_reserved_year_parent(self):
        def die(_):raise ProcessDeath()
        self.f.server.after_create = die
        with self.assertRaises(ProcessDeath):self.run_revision(apply=True)
        self.f.server.after_create = None
        count = len(self.f.server.created)
        result = self.run_revision(apply=True)
        self.assertEqual(result['counts'], {'APPLIED': 1}, result)
        self.assertEqual(len(self.f.server.created), count)


    def test_completed_file_revision_has_fresh_baseline_and_new_operations(self):
        # Undo only proposal edits in the fixture; live completed files remain.
        self.set_cell('Move Manifest', 1, 'Exact target path', self.old_path)
        self.set_cell('Proposed Moves', 1, 'Proposed target path', self.old_path)
        self.set_cell('Proposed Renames', 1, 'Proposed name + type suffix', '2020')
        self.set_cell('Proposed Renames', 1, 'Proposed destination folder', SECTION + '/Category')
        self.set_cell('Proposed Renames', 2, 'Proposed destination folder', self.old_path)
        self.set_cell('Proposed Renames', 2, 'Proposed name + type suffix', 'Revised.pdf')
        self.manifest = self.make_manifest()
        self.manifest['items'] = [{'source_id': 'leaf', 'before_path': self.old_path + '/Approved.pdf',
            'target_path': self.old_path + '/Revised.pdf', 'target_name': 'Revised.pdf'}]
        self.manifest['new_folders'] = []
        result = self.run_revision(apply=True)
        self.assertEqual(result['counts'], {'APPLIED': 1}, result)
        self.assertEqual(self.f.server.requests[-1], {'fileId': 'leaf', 'name': 'Revised.pdf'})
        history = build_history_index(Journal(self.f.config['store']))['leaf']
        self.assertEqual(len(history['plan_hashes']), 2)
        self.assertEqual(history['last_applied']['before']['name'], 'Approved.pdf')
        self.f.run_command(apply=True, rollback=result['run_id'])
        self.assertEqual(self.f.server.drive.files['leaf']['name'], 'Approved.pdf')

    def test_explicit_expiry_and_revocation_are_enforced(self):
        self.manifest['expires_at_utc'] = '2000-01-01T00:00:00Z'
        count = len(self.f.server.created)
        with self.assertRaisesRegex(SafetyError, 'SECTION_APPROVAL_EXPIRED'):
            self.run_revision(apply=True)
        self.assertEqual(len(self.f.server.created), count)
        del self.manifest['expires_at_utc']
        self.run_revision(apply=True)
        journal = Journal(self.f.config['store'])
        with journal.locked():
            origin = next(e for e in reversed(journal.records()) if e['kind'] == 'DESTINATION_REVISION_FROZEN')
            owner = journal.read_evidence(origin['payload']['owner_approval_ref'])
            journal.append('SECTION_APPROVAL_REVOKED', payload={'approval_id': owner['approval_id']})
        with self.assertRaisesRegex(SafetyError, 'REVOKED_OR_SUPERSEDED'):
            self.run_revision(apply=True, resume=True)

    def test_authority_cache_invalidated_by_new_revision_origin(self):
        from uqmc_recovery.__main__ import plan_from_json
        from uqmc_recovery.run_authority import RunAuthority
        self.run_revision(apply=True)
        saved = json.loads(Path(self.f.config['registry']).read_text())['runs'][-1]
        plan = plan_from_json(json.loads(Path(saved['plan_path']).read_text()))
        journal = Journal(self.f.config['store'])
        with journal.locked():
            cache = RunAuthority(journal, plan, plan.proposal_hash, False)
            journal.append('DESTINATION_REVISION_FROZEN', payload={'section_id': saved['section_id'],
                'proposal_hash': 'different-revision'})
            with self.assertRaisesRegex(SafetyError, 'AUTHORITY_BINDING_CHANGED'):
                cache.check()
        # A later origin must also prevent treating it as an unrelated update.
        self.assertEqual(self.f.server.requests[-1]['fileId'], self.folder)


if __name__ == '__main__':unittest.main()
