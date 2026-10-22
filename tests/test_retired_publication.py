"""Retired audit identities are not active work or verified migration effects."""
import base64
import copy
import gzip
import json
import unittest
from types import SimpleNamespace

import test_publication_projection as projection_fixture
import test_destination_publication as destination_fixture
from test_publication_recoverable_exclusion import missing
from uqmc_recovery import destination_publication as publication
from uqmc_recovery import viewer_publication as viewer
from uqmc_recovery.projection import validate_snapshot
from uqmc_recovery.publication_projection import build_publication, FOLDER
from uqmc_recovery.sheet_publication import build_sheet_plan, item_status, read_existing


def retired(source):
    return {'file_id': source, 'reason_code': 'USER_CONFIRMED_DELETED',
            'effect': 'NOT_APPLIED', 'proposal_ref': 'fixture-row',
            'original_mapping': {'name': 'missing.pdf', 'destination': 'Root/Section'},
            'evidence_ref': 'PRIVATE_RETIREMENT_EVIDENCE'}


class RetiredProjectionTests(unittest.TestCase):
    def setUp(self):
        self.f = projection_fixture.PublicationTests(methodName='runTest')
        self.f.setUp()
        self.f.applied()
        self.f.j.append('DESTINATION_VERIFIED', payload={
            'section_id': 'section', 'file_id': 'destination', 'exact_name': 'Destination',
            'parent_ids': ['anchor'], 'proposal_path': 'Anchor/Destination',
            'observed_ref': self.f.j.evidence({'id': 'destination', 'name': 'Destination',
                'parents': ['anchor'], 'mime_type': FOLDER})})
        self.spec = {'section_id': 'section', 'title': 'Destination',
            'proposal_path': 'Anchor/Destination', 'plan_sha256': self.f.ph,
            'source_ids': ['file', 'untouched'], 'destination_id': 'destination',
            'destination_mode': 'REUSED_EXISTING'}

    def test_explicit_retirement_removes_only_active_denominator_preserving_audit(self):
        f = self.f
        f.j.append('EXCLUDED', payload=missing('untouched'))
        before = f.publish(sections=[self.spec])
        f.j.append('RETIRED', payload=retired('untouched'))
        events = copy.deepcopy(f.j.events)
        bundle = f.publish(sections=[self.spec])
        section = bundle['sections']['sections'][0]
        self.assertEqual((section['status'], section['completed_file_count'],
            section['expected_file_count'], section['remaining_file_count']), ('COMPLETED', 1, 1, 0))
        self.assertEqual(section['retired_file_ids'], ['untouched'])
        self.assertEqual(section['retired_file_count'], 1)
        self.assertEqual(section['verified_file_ids'], ['file'])
        for surface in ('workbook', 'html'):
            validate_snapshot(bundle[surface])
            item = next(i for i in bundle[surface]['items'] if i['source_id'] == 'untouched')
            self.assertEqual(item['move']['effect'], 'NOT_TRACKED')
            self.assertEqual(item['rename']['effect'], 'NOT_TRACKED')
            self.assertEqual(item['observed']['read_state'], 'NEVER_VERIFIED')
            self.assertTrue({'EXCLUDED', 'RETIRED', 'MISSING_OR_INACCESSIBLE',
                'USER_CONFIRMED_DELETED', 'NOT_APPLIED'} <= set(item['reason_codes']))
        self.assertEqual(bundle['operation_log'], {**before['operation_log'],
            'snapshot_id': bundle['operation_log']['snapshot_id'],
            'journal_sequence': bundle['operation_log']['journal_sequence'],
            'journal_tip_sha256': bundle['operation_log']['journal_tip_sha256'],
            'generated_at_utc': bundle['operation_log']['generated_at_utc']})
        self.assertNotIn('PRIVATE_RETIREMENT_EVIDENCE', json.dumps(bundle['html']))
        self.assertEqual(f.j.events, events)

    def test_missing_without_retirement_remains_outstanding(self):
        self.f.j.append('EXCLUDED', payload=missing('untouched'))
        section = self.f.publish(sections=[self.spec])['sections']['sections'][0]
        self.assertEqual((section['status'], section['expected_file_count'],
            section['remaining_file_count'], section['retired_file_count']), ('PARTIAL', 2, 1, 0))

    def test_only_exact_retired_reason_and_effect_reduce_denominator(self):
        for change in ({'reason_code': 'MISSING_OR_INACCESSIBLE'}, {'effect': 'APPLIED'}):
            with self.subTest(change=change):
                self.f.j.append('RETIRED', payload={**retired('untouched'), **change})
                section = self.f.publish(sections=[self.spec])['sections']['sections'][0]
                self.assertEqual(section['expected_file_count'], 2)
                self.assertEqual(section['retired_file_count'], 0)

    def test_retirement_never_discloses_private_or_unallowlisted_identity(self):
        self.f.j.append('RETIRED', payload=retired('untouched'))
        bundle = build_publication(self.f.j, allowed_source_ids=['file', 'destination'],
            identity_basis={'identity_basis_version': 1, 'source_ids': self.f.allowed})
        self.assertNotIn('untouched', {i['source_id'] for i in bundle['html']['items']})
        self.f.j.append('EXCLUDED', payload={'file_id': 'untouched', 'reason_code': 'PRIVATE_WITHDRAWAL'})
        self.assertNotIn('untouched', {i['source_id'] for i in self.f.publish()['html']['items']})
        with self.assertRaisesRegex(ValueError, 'SECTION_IDENTITIES_NOT_PUBLIC_ALLOWLISTED'):
            self.f.publish(sections=[self.spec])

    def test_retirement_status_is_written_without_a_fake_verified_action(self):
        from test_sheet_publication import fixture_bundle, FakeSheets
        bundle = fixture_bundle()
        item = next(i for i in bundle['workbook']['items'] if i['source_id'] == 'untracked')
        item['reason_codes'] = ['RETIRED', 'USER_CONFIRMED_DELETED', 'NOT_APPLIED']
        self.assertEqual(item_status(item), 'RETIRED — user-confirmed deleted; not migrated')
        sheet = FakeSheets()
        capture = SimpleNamespace(service=sheet, workbook_id='fixture-workbook')
        original = copy.deepcopy(sheet.matrices)
        plan = build_sheet_plan(bundle, read_existing(capture))
        row = next(c for c in plan['changes'] if c['range'] == "'Execution Status'!C7:S7")['values'][0]
        self.assertEqual(row[:2], ['NOT_TRACKED', 'NOT_TRACKED'])
        self.assertIsNone(row[10])
        self.assertIn('RETIRED', row[-1])
        self.assertEqual(sheet.matrices, original)


class RetiredAutomaticPublicationTests(unittest.TestCase):
    def setUp(self):
        self.f = destination_fixture.DestinationPublicationTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        data = viewer.unpack(self.f.drive.content.decode())
        data['nodes'][3].update(p=1, s='destination')
        data['roots']['retained'] = []
        packed = base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()
        source = self.f.drive.content.decode()
        self.f.drive.content = viewer.TREE.sub(lambda m: m.group(1) + packed + m.group(3), source, count=1).encode()
        row = copy.deepcopy(self.f.sheet.matrices['Proposed Renames'][1])
        row[1:3] = ['Untracked', 'Untracked']
        row[4:6] = ['No', '=IF(E3="Yes","Approved","Declined")']
        row[13:17] = ['RENAME-AUDIT', '', '', 'untracked']
        self.f.sheet.matrices['Proposed Renames'].append(copy.deepcopy(row))
        self.f.workbook['sheets']['Proposed Renames'].append(copy.deepcopy(row))

    def publish(self):
        f = self.f
        return publication.publish_registered_sections(f.config, f.registry, journal=f.j,
            fresh_workbook=f.workbook, sheet_capture=f.capture, drive=f.drive, apply=True)

    def test_automatic_publication_and_retry_keep_retired_node_complete_active_section(self):
        f = self.f
        f.j.append('EXCLUDED', payload=missing('untracked'))
        f.j.append('RETIRED', payload=retired('untracked'))
        events = copy.deepcopy(f.j.events)
        result = self.publish()
        self.assertEqual(result['result'], 'PUBLICATION_VERIFIED', result)
        delivered = viewer.unpack(f.drive.content.decode())
        section = delivered['execution_sections']['sections'][0]
        self.assertEqual((section['status'], section['expected_file_count'],
            section['completed_file_count'], section['retired_file_count']), ('COMPLETED', 1, 1, 1))
        node = next(n for n in delivered['nodes'] if n.get('id') == 'untracked')
        self.assertTrue(node['retired'])
        summary = delivered['nodes'][1]['migration_summary']
        self.assertEqual((summary['status'], summary['file_count'], summary['completed_file_count'],
            summary['retired_file_count']), ('COMPLETE', 1, 1, 1))
        self.assertEqual(f.sheet.matrices['Migration Summary'][1][:4], ['Root/Section', 'COMPLETED', 1, 1])
        row = f.sheet.matrices['Proposed Renames'][2]
        self.assertEqual(row[4:6], ['No', '=IF(E3="Yes","Approved","Declined")'])
        self.assertEqual(row[18], 'RETIRED — user-confirmed deleted; not migrated')
        self.assertIn('1 retired (user-confirmed deleted; not migrated)', f.drive.content.decode())
        self.assertNotIn('PRIVATE_RETIREMENT_EVIDENCE', f.drive.content.decode())
        self.assertEqual(f.j.events, events)
        self.assertEqual(self.publish()['result'], 'PUBLICATION_VERIFIED')
        self.assertEqual((len(f.sheet.updates), len(f.drive.writes)), (1, 1))

    def test_generic_no_decision_is_not_retirement(self):
        f = self.f
        self.assertEqual(f.workbook['sheets']['Proposed Renames'][2][4], 'No')
        self.assertEqual(self.publish()['result'], 'PUBLICATION_VERIFIED')
        section = viewer.unpack(f.drive.content.decode())['execution_sections']['sections'][0]
        self.assertEqual((section['status'], section['expected_file_count'], section['retired_file_count']),
                         ('PARTIAL', 2, 0))

    def test_viewer_rejects_forged_retired_overlay_membership(self):
        f = self.f
        self.publish()
        data = viewer.unpack(f.drive.content.decode())
        # Re-integrate the delivered public envelope with forged retirement.
        bundle = {'html': data['execution'], 'sections': copy.deepcopy(data['execution_sections'])}
        bundle['sections']['sections'][0].update(retired_file_ids=['untracked'], retired_file_count=1)
        with self.assertRaisesRegex(ValueError, 'SECTION_RETIRED_MEMBERSHIP_MISMATCH'):
            viewer.integrate(data, bundle)

    def test_retired_badge_is_idempotent_on_existing_viewer_script(self):
        f = self.f
        f.j.append('RETIRED', payload=retired('untracked'))
        self.publish()
        data = viewer.unpack(f.drive.content.decode())
        source = f.drive.content.decode() + "<script>function executionBadges(n){const wrap=el('div','execution-badges');if(!n.id)return wrap;return wrap;}</script>"
        once = viewer.render(source, data)
        twice = viewer.render(once, data)
        self.assertEqual(twice.count('Retired · user-confirmed deleted · not migrated'), 1)


if __name__ == '__main__':
    unittest.main()
