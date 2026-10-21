"""MIME-backed uploaded display labels require exact independent source evidence."""
import copy
import unittest

import test_destination_command as fixtures
from test_importer import append_row, set_cell
from uqmc_recovery.destination import proposal_original_name
from uqmc_recovery.journal import Journal

DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def evidence(title='Original title', *, mime=DOCX, display=None):
    path = 'Archive/Original folder/' + title
    rename = {'source_id': 'fixture-document', 'row': {
        'Current name + type suffix': title + '.docx' if display is None else display,
        'Current source path': path, 'Proposed name + type suffix': 'desired.docx'}}
    move = {'source_id': 'fixture-document', 'row': {
        'Current item': title, 'Source path': path}}
    state = {'id': 'fixture-document', 'name': title, 'mime_type': mime}
    return rename, move, state


class UploadedDisplayTitleTests(unittest.TestCase):
    def test_eleven_synthetic_docx_titles_resolve_exactly_without_changing_targets(self):
        titles = ['Meeting notes', 'Agenda 01', 'Committee record', 'Annual review',
                  'Planning notes', 'Minutes copy', 'Draft minutes', 'Review (final)',
                  'Notes and actions', 'Leading and trailing ', 'Mixed CASE title']
        self.assertEqual(len(titles), 11)
        for title in titles:
            with self.subTest(title=title):
                records = evidence(title)
                original = copy.deepcopy(records)
                self.assertEqual(proposal_original_name(*records), title)
                self.assertEqual(records, original)
                self.assertEqual(records[0]['row']['Proposed name + type suffix'], 'desired.docx')

    def test_missing_or_mismatched_identity_evidence_keeps_proposal_baseline(self):
        for missing in (False, True):
            for index, field in ((0, 'source_id'), (1, 'source_id'), (2, 'id')):
                with self.subTest(missing=missing, record=index):
                    records = evidence()
                    if missing:
                        records[index].pop(field)
                    else:
                        records[index][field] = 'unrelated-identity'
                    self.assertEqual(proposal_original_name(*records), 'Original title.docx')

    def test_missing_or_inexact_title_and_path_evidence_never_hides_drift(self):
        for index, field in ((0, 'Current source path'), (1, 'Current item'), (1, 'Source path')):
            for replacement in (None, '', 'Other/Original title changed', 'Original title'):
                with self.subTest(record=index, field=field, replacement=replacement):
                    records = evidence()
                    if replacement is None:
                        records[index]['row'].pop(field)
                    else:
                        records[index]['row'][field] = replacement
                    # Current item must itself be exact, so the exact title is
                    # not a negative case for that one field.
                    if field == 'Current item' and replacement == 'Original title':
                        continue
                    self.assertEqual(proposal_original_name(*records), 'Original title.docx')
        rename, move, state = evidence()
        self.assertEqual(proposal_original_name(rename, None, state), 'Original title.docx')
        state['name'] = 'A human changed this title'
        self.assertEqual(proposal_original_name(rename, move, state), 'Original title.docx')

    def test_whitespace_case_and_wrong_mime_are_not_generically_normalized(self):
        cases = [evidence('Original title ', display='Original title.docx'),
                 evidence(display='Original title.DOCX'),
                 evidence(display='original title.docx'),
                 evidence(mime='application/pdf'),
                 evidence(mime='application/octet-stream'),
                 evidence(mime='application/vnd.google-apps.folder'),
                 evidence(display='Original title.gdoc')]
        for records in cases:
            with self.subTest(state=records[2], display=records[0]['row']['Current name + type suffix']):
                self.assertEqual(proposal_original_name(*records),
                                 records[0]['row']['Current name + type suffix'])

    def test_real_uploaded_extension_remains_in_exact_historical_name(self):
        for display in ('Original.docx', 'Original.docx.docx'):
            with self.subTest(display=display):
                self.assertEqual(proposal_original_name(*evidence('Original.docx', display=display)),
                                 'Original.docx')

    def test_existing_native_display_and_literal_suffix_behavior_is_unchanged(self):
        for current, display, expected in (('Native title', 'Native title.gdoc', 'Native title'),
                                           ('Native.gdoc', 'Native.gdoc', 'Native.gdoc')):
            records = evidence(current, mime='application/vnd.google-apps.document', display=display)
            self.assertEqual(proposal_original_name(*records), expected)

    def test_fake_cli_apply_and_rollback_record_exact_extensionless_before_title(self):
        fixture = fixtures.DestinationCommandTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        item = fixture.server.drive.files['leaf']
        item.update(name='Original title', mimeType=DOCX)
        path = 'Archive/Old folder/Original title'
        for field, value in {
            'Current name + type suffix': 'Original title.docx',
            'Current source path': path,
            'Proposed name + type suffix': 'desired.docx',
        }.items():
            set_cell(fixture.export, 'Proposed Renames', 2, field, value)
        append_row(fixture.export, 'Proposed Moves', {'Proposal ID': 'MOVE-LEAF',
            'Action': 'Move', 'Proposed target path': fixtures.SECTION})
        append_row(fixture.export, 'Move Manifest', {'Proposal ID': 'MOVE-LEAF',
            'Manifest item': 'ITEM-LEAF', 'Source ID': 'leaf', 'Current item': 'Original title',
            'Source path': path, 'Exact target path': fixtures.SECTION + '/Original title',
            'Scope': 'Single item'})
        item['name'] = 'A human changed this title'
        rejected = fixture.run_command()
        self.assertEqual(rejected['blockers'][0]['reason'], 'UNAPPROVED_CURRENT_STATE:name')
        self.assertEqual(fixture.server.requests, [])
        item['name'] = 'Original title'
        result = fixture.run_command(apply=True)
        self.assertEqual(result['counts'], {'APPLIED': 2}, result)
        self.assertEqual(fixture.server.drive.files['leaf']['name'], 'desired.docx')
        journal = Journal(fixture.config['store'])
        event = next(e for e in journal.records() if e['kind'] == 'DESIRED_PREPARED'
                     and e['payload']['source_id'] == 'leaf')
        self.assertEqual(journal.read_evidence(event['payload']['before_ref'])['name'], 'Original title')
        self.assertEqual(event['payload']['args'], {'fileId': 'leaf', 'name': 'desired.docx'})
        restored = fixture.run_command(apply=True, rollback=result['run_id'])
        self.assertEqual(restored['counts'], {'ROLLED_BACK': 2}, restored)
        self.assertEqual(fixture.server.drive.files['leaf']['name'], 'Original title')
        self.assertEqual(fixture.server.drive.files['leaf']['parents'], ['source-root'])
        self.assertEqual(fixture.server.drive.files['source-root']['parents'], ['old-parent'])


if __name__ == '__main__':
    unittest.main()
