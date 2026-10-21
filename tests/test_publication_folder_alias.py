"""Offline publication regressions for canonical folder aliases and child runs."""
import base64
import copy
import gzip
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from test_destination_publication import fixture, FakeDrive
from uqmc_recovery import destination_publication as p
from uqmc_recovery import viewer_publication as v


def pack(data):
    encoded = base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()
    return ('<html><style></style><script id="tree-data">' + encoded +
            '</script><section class="execution-summary"></section></html>').encode()


class FolderAliasPublicationTests(unittest.TestCase):
    def alias_fixture(self):
        journal, registry, html, sheet, workbook = fixture()
        data = v.unpack(html.decode())
        data['nodes'][1].update(n='Section - Source Media', o='Section', destination_overlay={
            'destination_id': 'destination',
            'destination_url': 'https://drive.google.com/drive/folders/destination',
            'mode': 'REUSED_EXISTING', 'section_id': 'section'})
        raw = {'id': 'destination', 'name': 'Section - Source Media', 'parents': ['anchor'],
               'mime_type': 'application/vnd.google-apps.folder', 'trashed': False}
        journal.append('DESTINATION_VERIFIED', payload={'section_id': 'section',
            'file_id': 'destination', 'exact_name': raw['name'], 'parent_ids': ['anchor'],
            'proposal_path': 'Root/Section', 'observed_ref': journal.evidence(raw)})
        return journal, registry, data, sheet, workbook

    def test_resolver_accepts_old_new_and_mixed_component_aliases(self):
        _, _, data, _, _ = self.alias_fixture()
        data['nodes'][0].update(n='Root - New', o='Root')
        for path in ('Root/Section', 'Root - New/Section - Source Media',
                     'Root/Section - Source Media', 'Root - New/Section'):
            with self.subTest(path=path):
                self.assertEqual(v.resolve_folder_path(data, path)['i'], 1)

    def test_full_publication_uses_alias_without_adding_or_rebinding_identities(self):
        journal, registry, data, sheet, workbook = self.alias_fixture()
        original_registry = copy.deepcopy(registry)
        original_ids = {n['id'] for n in data['nodes'] if n.get('id')}
        drive = FakeDrive(pack(data))
        capture = SimpleNamespace(service=sheet, workbook_id='fixture-workbook')
        with tempfile.TemporaryDirectory() as directory:
            config = {'publication_directory': directory,
                'identity_basis': {'identity_basis_version': 1, 'source_ids': ['source-one', 'untracked']},
                'workbook_id': 'fixture-workbook', 'viewer_file_id': 'viewer'}
            result = p.publish_registered_sections(config, registry, journal=journal,
                fresh_workbook=workbook, sheet_capture=capture, drive=drive, apply=True)
            self.assertEqual(result['result'], 'PUBLICATION_VERIFIED', result)
            rendered = v.unpack(drive.content.decode())
            self.assertEqual({n['id'] for n in rendered['nodes'] if n.get('id')}, original_ids)
            node = v.resolve_folder_path(rendered, 'Root/Section')
            self.assertEqual(node['destination_overlay']['destination_id'], 'destination')
            self.assertEqual(node['n'], 'Section - Source Media')
            self.assertEqual(node['migration_summary']['status'], 'COMPLETE')
            self.assertEqual(rendered['execution_sections']['sections'][0]['completed_file_count'], 1)
            self.assertEqual(registry, original_registry)
            self.assertEqual(len(drive.writes), 1)
            self.assertEqual(len(sheet.updates), 1)
            receipt = json.loads((Path(result['directory']) / 'publication.json').read_text())
            self.assertEqual(receipt['sheet']['state'], 'VERIFIED')
            self.assertEqual(receipt['html']['state'], 'VERIFIED')

    def test_duplicate_original_alias_fails_before_publication(self):
        _, registry, data, _, _ = self.alias_fixture()
        duplicate = copy.deepcopy(data['nodes'][1])
        duplicate.update(i=4, n='Another folder', o='Section')
        data['nodes'].append(duplicate)
        with self.assertRaises(ValueError):
            v.resolve_folder_path(data, 'Root/Section')
        with self.assertRaises(ValueError):
            p.display_section_specs(registry, data, ['source-one', 'untracked'])

    def test_child_batch_hashes_roll_up_only_to_containing_section_display(self):
        def node(i, parent, name, folder, sid=''):
            return dict(i=i, p=parent, n=name, o=None, f=folder, id=sid,
                        s='destination', c=[], changed=False, review=False)
        nodes = [node(0, -1, 'Root', True), node(1, 0, 'Existing.txt', False, 'existing'),
                 node(2, 0, 'Media - Source', True)]
        nodes[2]['o'] = 'Media'
        child_ids = ['media-%02d' % n for n in range(50)]
        for sid in child_ids:
            nodes.append(node(len(nodes), 2, sid + '.mov', False, sid))
        outside_root = len(nodes)
        nodes.append(node(outside_root, -1, 'Outside', True))
        nodes.append(node(len(nodes), outside_root, 'Other.txt', False, 'outside-file'))
        def spec(sid, path, ids, plan):
            return {'section_id': sid, 'title': path, 'proposal_path': path,
                'source_ids': ids, 'plan_sha256': plan, 'destination_id': sid + '-destination',
                'destination_mode': 'REUSED_EXISTING'}
        registry = {'sections': [
            {'publication_spec': spec('parent', 'Root', ['existing'], 'a' * 64)},
            {'publication_spec': spec('child', 'Root/Media', child_ids, 'b' * 64)},
            {'publication_spec': spec('outside', 'Outside', ['outside-file'], 'c' * 64)}]}
        before = copy.deepcopy(registry)
        specs = {s['section_id']: s for s in p.display_section_specs(
            registry, {'nodes': nodes}, ['existing', 'outside-file', *child_ids])}
        self.assertEqual(set(specs['parent']['source_ids']), {'existing', *child_ids})
        self.assertEqual(set(specs['parent']['plan_sha256s']), {'a' * 64, 'b' * 64})
        self.assertEqual(set(specs['child'].get('plan_sha256s', [specs['child']['plan_sha256']])), {'b' * 64})
        self.assertEqual(set(specs['outside'].get('plan_sha256s', [specs['outside']['plan_sha256']])), {'c' * 64})
        self.assertEqual(registry, before)


if __name__ == '__main__':
    unittest.main()
