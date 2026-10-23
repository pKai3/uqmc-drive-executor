"""Explicit viewer identity registration, with synthetic identities only."""
import copy
import unittest
from pathlib import Path

from test_destination_publication import DestinationPublicationTests
from uqmc_recovery import destination_publication as p
from uqmc_recovery import viewer_publication as v


class ViewerRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DestinationPublicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.config['identity_basis']['source_ids'].append('new-document')
        self.entry = dict(source_id='new-document', rename_id='RENAME-NEW',
            original_name='Original / title', current_display_name='Original / title.gdoc',
            approval_reference='explicit-user-approval')
        f.config['identity_basis']['viewer_registrations'] = [self.entry]
        rows = f.workbook['sheets']['Proposed Renames']
        for key in ('Rename ID', 'Current name + type suffix'):
            if key not in rows[0]:
                for row in rows[1:]:
                    row.extend([''] * (len(rows[0]) - len(row)))
                    row.append('')
                rows[0].append(key)
        record = {'Source ID':'new-document', 'Rename ID':'RENAME-NEW',
            'Current name + type suffix':'Original / title.gdoc',
            'Proposed name + type suffix':'Approved title.gdoc',
            'Proposed destination folder':'Root/Section'}
        rows.append([record.get(k, '') for k in rows[0]])
        f.registry['sections'][0]['publication_spec']['source_ids'].append('new-document')

    def test_registration_clears_missing_member_without_publication_or_false_completion(self):
        f = self.fixture
        before = copy.deepcopy(f.j.events)
        path = f.freeze()
        data = v.unpack((path / 'viewer.html').read_text())
        node = next(n for n in data['nodes'] if n.get('id') == 'new-document')
        self.assertEqual((node['n'], node['o']), ('Approved title.gdoc', 'Original / title'))
        section = data['execution_sections']['sections'][0]
        self.assertEqual((section['status'], section['completed_file_count'], section['expected_file_count']),
                         ('PARTIAL', 1, 2))
        self.assertEqual(before, f.j.events)
        self.assertFalse(f.drive.writes)
        self.assertFalse(f.sheet.updates)
        self.assertEqual(p._load(path / 'publication.json')['proposal_report']['registered_source_ids'], ['new-document'])

    def test_idempotent_with_existing_registered_node(self):
        f = self.fixture
        data = v.unpack(f.drive.content.decode())
        self.assertEqual(v.register_proposal_files(data, f.workbook, f.config['identity_basis']), ['new-document'])
        v.refresh_proposal(data, f.workbook)
        saved = copy.deepcopy(data)
        self.assertEqual(v.register_proposal_files(data, f.workbook, f.config['identity_basis']), [])
        self.assertEqual(data, saved)

    def test_plan_presence_alone_is_not_disclosure_authority(self):
        f = self.fixture
        f.config['identity_basis'].pop('viewer_registrations')
        with self.assertRaisesRegex(ValueError, 'SECTION_REGISTERED_MEMBER_MISSING'):
            f.freeze()

    def test_registration_must_be_allowlisted_and_unique(self):
        f = self.fixture
        f.config['identity_basis']['source_ids'].remove('new-document')
        with self.assertRaisesRegex(ValueError, 'VIEWER_REGISTRATION_INVALID'):
            f.freeze()
        f.config['identity_basis']['source_ids'].append('new-document')
        f.config['identity_basis']['viewer_registrations'].append(copy.deepcopy(self.entry))
        with self.assertRaisesRegex(ValueError, 'VIEWER_REGISTRATION_INVALID'):
            f.freeze()

    def test_wrong_or_ambiguous_row_is_rejected(self):
        f = self.fixture
        self.entry['rename_id'] = 'another-row'
        with self.assertRaisesRegex(ValueError, 'VIEWER_REGISTRATION_ROW_MISMATCH'):
            f.freeze()
        self.entry['rename_id'] = 'RENAME-NEW'
        rows = f.workbook['sheets']['Proposed Renames']
        rows.append(copy.deepcopy(rows[-1]))
        with self.assertRaisesRegex(ValueError, 'VIEWER_REGISTRATION_ROW_NOT_UNIQUE'):
            f.freeze()

    def test_existing_folder_identity_cannot_be_reclassified(self):
        f = self.fixture
        data = v.unpack(f.drive.content.decode())
        data['nodes'][1]['id'] = 'new-document'
        with self.assertRaisesRegex(ValueError, 'VIEWER_REGISTRATION_ID_CONFLICT'):
            v.register_proposal_files(data, f.workbook, f.config['identity_basis'])


if __name__ == '__main__':
    unittest.main()
