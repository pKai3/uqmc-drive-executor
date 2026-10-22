"""No-op destination proofs may fill missing actions, never hide changed targets."""
import base64
import copy
import gzip
import json
import unittest

import test_destination_publication as fixture_module
from uqmc_recovery import destination_publication as publication
from uqmc_recovery import viewer_publication as viewer
from uqmc_recovery.projection import validate_snapshot


class NoopDestinationPublicationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture_module.DestinationPublicationTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ph = 'b' * 64
        self.folder = {'id': 'nested', 'name': 'Nested', 'parents': ['destination'],
                       'mime_type': 'application/vnd.google-apps.folder', 'trashed': False}
        self.file_before = {'id': 'source-one', 'name': 'New.txt',
                            'parents': ['destination'], 'mime_type': 'text/plain'}
        self.file_target = {**self.file_before, 'parents': ['nested']}
        self.spec = self.f.registry['sections'][0]['publication_spec']

    def prepare_nested(self, *, folder_target=None, observed_path='Root/Section/Nested'):
        j = self.f.j
        j.append('DESTINATION_VERIFIED', payload={
            'section_id': 'section', 'file_id': 'nested', 'exact_name': 'Nested',
            'parent_ids': ['destination'], 'proposal_path': observed_path,
            'observed_ref': j.evidence(self.folder)})
        folder_target = folder_target or {'name': 'Nested', 'parents': ['destination']}
        ops = [
            {'operation_id': 'noop-folder', 'file_id': 'nested', 'action': 'MOVE',
             'before': self.folder, 'target': folder_target, 'depends_on': []},
            {'operation_id': 'move-child', 'file_id': 'source-one', 'action': 'MOVE',
             'before': self.file_before, 'target': {'parents': ['nested']}, 'depends_on': []}]
        j.append('DRAFTED', payload={'proposal_hash': self.ph,
            'plan_ref': j.evidence({'operations': ops})})
        j.append('DESIRED_APPLIED', 'desired-child', 1, {
            'run_id': 'nested-run', 'plan_sha256': self.ph, 'source_id': 'source-one',
            'direction': 'FORWARD', 'operation_order': 2, 'dependencies': [],
            'original_operation_ids': ['move-child'],
            'before_ref': j.evidence(self.file_before), 'target_ref': j.evidence(self.file_target),
            'observed_ref': j.evidence(self.file_target),
            'args': {'addParents': 'nested', 'removeParents': 'destination'}, 'audit_permissions': False})
        self.spec['plan_sha256s'] = [self.spec.pop('plan_sha256'), self.ph]
        self.spec['destination_ids'] = ['destination', 'nested']
        self.spec['destination_paths'] = {'destination': 'Root/Section', 'nested': 'Root/Section/Nested'}
        self.f.config['identity_basis']['source_ids'].append('nested')

    def bundle(self):
        return publication.build_publication(self.f.j,
            allowed_source_ids=self.f.config['identity_basis']['source_ids'],
            identity_basis=self.f.config['identity_basis'], sections=publication.section_specs(self.f.registry))

    def test_noop_nested_folder_proof_allows_verified_children_and_automatic_publication(self):
        self.prepare_nested()
        data = viewer.unpack(self.f.drive.content.decode())
        child = copy.deepcopy(data['nodes'][1])
        child.update(i=2, p=1, n='Nested', id='nested')
        data['nodes'][2].update(i=3, p=2)
        data['nodes'][3]['i'] = 4
        data['nodes'].insert(2, child)
        data['roots']['retained'] = [4]
        packed = base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()
        source = self.f.drive.content.decode()
        self.f.drive.content = viewer.TREE.sub(lambda m: m.group(1) + packed + m.group(3), source, count=1).encode()
        self.f.workbook['sheets']['Proposed Renames'][1][6] = 'Root/Section/Nested'
        bundle = self.bundle()
        validate_snapshot(bundle['workbook'])
        validate_snapshot(bundle['html'])
        section = bundle['sections']['sections'][0]
        self.assertEqual(section['status'], 'COMPLETED')
        nested = next(d for d in section['verified_destinations'] if d['id'] == 'nested')
        self.assertEqual(nested['mode'], 'REUSED_EXISTING')
        self.assertEqual(nested['path'], 'Root/Section/Nested')
        self.assertFalse(any(op['source_id'] == 'nested' for op in bundle['workbook']['operations']))
        before = copy.deepcopy(self.f.j.events)
        result = publication.publish_registered_sections(
            self.f.config, self.f.registry, journal=self.f.j, fresh_workbook=self.f.workbook,
            sheet_capture=self.f.capture, drive=self.f.drive, apply=True)
        self.assertEqual(result['result'], 'PUBLICATION_VERIFIED', result)
        delivered = viewer.unpack(self.f.drive.content.decode())
        self.assertEqual(delivered['nodes'][2]['migration_summary']['status'], 'COMPLETE')
        self.assertEqual(delivered['nodes'][2]['destination_overlay']['mode'], 'REUSED_EXISTING')
        self.assertEqual(self.f.j.events, before)

    def test_old_observation_cannot_satisfy_changed_parent_target(self):
        self.prepare_nested(folder_target={'parents': ['other-destination']})
        section = self.bundle()['sections']['sections'][0]
        self.assertNotEqual(section['status'], 'COMPLETED')
        self.assertNotIn('nested', {d['id'] for d in section['verified_destinations']})

    def test_old_observation_cannot_satisfy_changed_name_target(self):
        self.prepare_nested(folder_target={'name': 'Renamed Nested'})
        section = self.bundle()['sections']['sections'][0]
        self.assertNotEqual(section['status'], 'COMPLETED')
        self.assertNotIn('nested', {d['id'] for d in section['verified_destinations']})

    def test_observation_must_match_exact_registry_path(self):
        self.prepare_nested(observed_path='Root/Section/Nested ')
        section = self.bundle()['sections']['sections'][0]
        self.assertNotIn('nested', {d['id'] for d in section['verified_destinations']})

    def test_prior_proof_does_not_mask_later_uncertain_action(self):
        self.prepare_nested()
        self.f.j.append('DESIRED_STOPPED', 'desired-nested', 1, {
            'run_id': 'nested-run', 'plan_sha256': self.ph, 'source_id': 'nested',
            'direction': 'FORWARD', 'operation_order': 1, 'dependencies': [],
            'original_operation_ids': ['noop-folder'], 'state': 'UNCERTAIN'})
        section = self.bundle()['sections']['sections'][0]
        self.assertNotIn('nested', {d['id'] for d in section['verified_destinations']})
        self.assertNotEqual(section['status'], 'COMPLETED')

    def test_noop_root_uses_exact_destination_proof_without_action(self):
        root = {'id': 'destination', 'name': 'Section', 'parents': ['anchor'],
                'mime_type': 'application/vnd.google-apps.folder'}
        self.f.j.append('DRAFTED', payload={'proposal_hash': self.ph,
            'plan_ref': self.f.j.evidence({'operations': [{
                'operation_id': 'noop-root', 'file_id': 'destination', 'action': 'MOVE',
                'before': root, 'target': {'parents': ['anchor']}, 'depends_on': []}]})})
        self.spec['plan_sha256s'] = [self.spec.pop('plan_sha256'), self.ph]
        section = self.bundle()['sections']['sections'][0]
        self.assertEqual(section['status'], 'COMPLETED')
        self.assertEqual(section['verified_destinations'][0]['mode'], 'REUSED_EXISTING')


if __name__ == '__main__':
    unittest.main()
