"""Public recovery status is not permission to execute or disclose private IDs."""
import base64
import copy
import gzip
import json
import unittest

import test_publication_projection as projection_fixture
import test_destination_publication as destination_fixture
from uqmc_recovery import destination_publication as publication
from uqmc_recovery import viewer_publication as viewer
from uqmc_recovery.publication_projection import build_publication
from uqmc_recovery.projection import validate_snapshot


def missing(source):
    return {'file_id': source, 'recoverable': True, 'effect': 'NOT_APPLIED',
            'reason_code': 'MISSING_OR_INACCESSIBLE', 'evidence_ref': 'PRIVATE_EVIDENCE'}


class RecoverableExclusionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = projection_fixture.PublicationTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_allowlisted_missing_member_stays_visible_and_section_partial(self):
        f = self.fixture
        f.applied()
        f.j.append('EXCLUDED', payload=missing('untouched'))
        spec = {'section_id': 'section', 'title': 'Destination',
                'proposal_path': 'Anchor/Destination', 'plan_sha256': f.ph,
                'source_ids': ['file', 'untouched'], 'destination_id': 'destination',
                'destination_mode': 'CREATED'}
        before = copy.deepcopy(f.j.events)
        bundle = f.publish(sections=[spec])
        section = bundle['sections']['sections'][0]
        self.assertEqual((section['status'], section['completed_file_count'],
                          section['expected_file_count'], section['remaining_file_count']),
                         ('PARTIAL', 1, 2, 1))
        for surface in ('workbook', 'html'):
            validate_snapshot(bundle[surface])
            item = next(i for i in bundle[surface]['items'] if i['source_id'] == 'untouched')
            self.assertEqual(item['rename']['effect'], 'NOT_TRACKED')
            self.assertEqual(item['move']['effect'], 'NOT_TRACKED')
            self.assertEqual(item['observed']['read_state'], 'NEVER_VERIFIED')
            self.assertTrue({'EXCLUDED', 'MISSING_OR_INACCESSIBLE', 'NOT_APPLIED'} <= set(item['reason_codes']))
            self.assertNotIn('WITHDRAWN', item['reason_codes'])
        self.assertNotIn('PRIVATE_EVIDENCE', json.dumps(bundle['html']))
        self.assertEqual(f.j.events, before)

    def test_missing_status_is_written_without_inventing_verification(self):
        from uqmc_recovery.sheet_publication import build_sheet_plan, item_status, read_existing
        from types import SimpleNamespace
        from test_sheet_publication import fixture_bundle, FakeSheets
        bundle = fixture_bundle()
        item = next(i for i in bundle['workbook']['items'] if i['source_id'] == 'untracked')
        item['reason_codes'] = ['EXCLUDED', 'MISSING_OR_INACCESSIBLE', 'NOT_APPLIED']
        self.assertEqual(item_status(item), 'EXCLUDED — missing or inaccessible; not migrated')
        sheet = FakeSheets()
        capture = SimpleNamespace(service=sheet, workbook_id='fixture-workbook')
        plan = build_sheet_plan(bundle, read_existing(capture))
        row = next(c for c in plan['changes'] if c['range'] == "'Execution Status'!C7:S7")['values'][0]
        self.assertEqual(row[:2], ['NOT_TRACKED', 'NOT_TRACKED'])
        self.assertIn('MISSING_OR_INACCESSIBLE', row[-1])
        self.assertIsNone(row[10])

    def test_missing_flag_never_adds_identity_to_public_allowlist(self):
        f = self.fixture
        f.j.append('EXCLUDED', payload=missing('untouched'))
        bundle = build_publication(f.j, allowed_source_ids=['file', 'destination'],
            identity_basis={'identity_basis_version': 1, 'source_ids': f.allowed})
        self.assertNotIn('untouched', {i['source_id'] for i in bundle['html']['items']})
        self.assertNotIn('untouched', json.dumps(bundle['sections']))

    def test_default_or_withdrawn_exclusion_always_wins_in_either_order(self):
        for hard in ({'file_id': 'untouched'},
                     {'file_id': 'untouched', 'reason_code': 'PERMANENT_WITHDRAWAL'}):
            for recoverable_first in (True, False):
                with self.subTest(hard=hard, recoverable_first=recoverable_first):
                    f = projection_fixture.PublicationTests(methodName='runTest')
                    f.setUp()
                    payloads = [missing('untouched'), hard]
                    if not recoverable_first:
                        payloads.reverse()
                    for payload in payloads:
                        f.j.append('EXCLUDED', payload=payload)
                    bundle = f.publish()
                    self.assertNotIn('untouched', {i['source_id'] for i in bundle['html']['items']})
                    self.assertIn('WITHDRAWN', f.item(bundle, 'untouched')['reason_codes'])
                    spec = {'section_id': 'section', 'title': 'Destination',
                        'proposal_path': 'Anchor/Destination', 'plan_sha256': f.ph,
                        'source_ids': ['untouched'], 'destination_id': 'destination',
                        'destination_mode': 'CREATED'}
                    with self.assertRaisesRegex(ValueError, '^SECTION_IDENTITIES_NOT_PUBLIC_ALLOWLISTED$'):
                        f.publish(sections=[spec])

    def test_display_exception_requires_all_three_exact_classification_fields(self):
        for change in ({'recoverable': False}, {'recoverable': 'true'}, {'effect': None},
                       {'effect': 'APPLIED'}, {'reason_code': 'PERMANENT_WITHDRAWAL'},
                       {'reason_code': None}):
            with self.subTest(change=change):
                f = projection_fixture.PublicationTests(methodName='runTest')
                f.setUp()
                f.j.append('EXCLUDED', payload={**missing('untouched'), **change})
                self.assertNotIn('untouched', {i['source_id'] for i in f.publish()['html']['items']})

    def test_excluded_member_is_not_counted_complete_from_earlier_observation(self):
        f = self.fixture
        f.applied()
        f.j.append('EXCLUDED', payload=missing('file'))
        spec = {'section_id': 'section', 'title': 'Destination',
            'proposal_path': 'Anchor/Destination', 'plan_sha256': f.ph,
            'source_ids': ['file'], 'destination_id': 'destination', 'destination_mode': 'CREATED'}
        bundle = f.publish(sections=[spec])
        self.assertEqual(bundle['sections']['sections'][0]['completed_file_count'], 0)
        self.assertEqual(bundle['sections']['sections'][0]['remaining_file_count'], 1)
        # Historical verified effects remain history; exclusion does not rewrite them.
        self.assertEqual(f.item(bundle)['move']['effect'], 'APPLIED')
        self.assertIn('EXCLUDED', f.item(bundle)['reason_codes'])


class RecoverableExclusionAutomaticPublicationTests(unittest.TestCase):
    def test_normal_freeze_and_resume_publish_partial_section_to_both_surfaces(self):
        f = destination_fixture.DestinationPublicationTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        data = viewer.unpack(f.drive.content.decode())
        data['nodes'][3].update(p=1, s='destination')
        data['roots']['retained'] = []
        packed = base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()
        source = f.drive.content.decode()
        f.drive.content = viewer.TREE.sub(lambda m: m.group(1) + packed + m.group(3), source, count=1).encode()
        f.j.append('EXCLUDED', payload=missing('untracked'))
        old_registry = copy.deepcopy(f.registry)
        before = copy.deepcopy(f.j.events)
        result = publication.publish_registered_sections(
            f.config, f.registry, journal=f.j, fresh_workbook=f.workbook,
            sheet_capture=f.capture, drive=f.drive, apply=True)
        self.assertEqual(result['result'], 'PUBLICATION_VERIFIED', result)
        self.assertEqual(len(f.sheet.updates), 1)
        self.assertEqual(len(f.drive.writes), 1)
        delivered = viewer.unpack(f.drive.content.decode())
        section = delivered['execution_sections']['sections'][0]
        self.assertEqual((section['status'], section['completed_file_count'], section['expected_file_count']),
                         ('PARTIAL', 1, 2))
        item = next(i for i in delivered['execution']['items'] if i['source_id'] == 'untracked')
        self.assertIn('EXCLUDED', item['reason_codes'])
        self.assertEqual(item['move']['effect'], 'NOT_TRACKED')
        self.assertEqual(delivered['nodes'][1]['migration_summary']['status'], 'PARTIAL')
        summary = f.sheet.matrices['Migration Summary'][1]
        self.assertEqual(summary[:4], ['Root/Section', 'PARTIAL', 1, 2])
        self.assertEqual(f.registry, old_registry)
        self.assertEqual(f.j.events, before)
        # Same automatic path recovers without replaying either publication write.
        repeated = publication.publish_registered_sections(
            f.config, f.registry, journal=f.j, fresh_workbook=f.workbook,
            sheet_capture=f.capture, drive=f.drive, apply=True)
        self.assertEqual(repeated['result'], 'PUBLICATION_VERIFIED')
        self.assertEqual(len(f.sheet.updates), 1)
        self.assertEqual(len(f.drive.writes), 1)
        self.assertNotIn('PRIVATE_EVIDENCE', json.dumps(delivered))


if __name__ == '__main__':
    unittest.main()
