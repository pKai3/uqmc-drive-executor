import copy
import unittest
from test_destination_publication import DestinationPublicationTests
from uqmc_recovery import destination_publication as p
from uqmc_recovery import viewer_publication as v

class DisplayMembershipTests(DestinationPublicationTests):
    def add_untracked(self):
        data=v.unpack(self.drive.content.decode())
        data['nodes'][3].update(p=1,s='destination')
        self.drive.content=v.render(self.drive.content.decode(), data).encode() if 'execution_sections' in data else self.repack(data)
        return data

    def repack(self,data):
        import base64,gzip,json
        packed=base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()
        return ('<html><style></style><script id="tree-data">'+packed+'</script><section class="execution-summary"></section></html>').encode()

    def test_extra_viewer_member_is_untracked_partial_and_registry_unchanged(self):
        self.add_untracked();before=copy.deepcopy(self.registry)
        path=self.freeze();bundle=p._load(path/'bundle.json')
        section=bundle['sections']['sections'][0]
        self.assertEqual((section['status'],section['completed_file_count'],section['expected_file_count'],section['remaining_file_count']),('PARTIAL',1,2,1))
        self.assertEqual(self.registry,before)
        extra=next(i for i in bundle['workbook']['items'] if i['source_id']=='untracked')
        self.assertEqual(extra['move']['effect'],'NOT_TRACKED')
        self.assertEqual(extra['observed']['read_state'],'NEVER_VERIFIED')
        data=v.unpack((path/'viewer.html').read_text())
        self.assertEqual(data['execution_sections']['sections'][0],section)
        self.assertEqual(data['nodes'][1]['migration_summary']['status'],'PARTIAL')
        self.assertEqual(data['nodes'][1]['migration_summary']['completed_file_count'],1)
        self.assertEqual(bundle['workbook']['snapshot_id'],bundle['html']['snapshot_id'])

    def test_registered_member_outside_section_still_blocks(self):
        data=v.unpack(self.drive.content.decode());data['nodes'][2].update(p=-1,s='retained')
        self.drive.content=self.repack(data)
        # Omit rename refresh, so this checks the actual proposed viewer location.
        self.workbook['sheets']['Proposed Renames']=self.workbook['sheets']['Proposed Renames'][:1]
        with self.assertRaisesRegex(ValueError,'SECTION_REGISTERED_MEMBER_MISSING'):self.freeze()

    def test_missing_registered_identity_still_blocks(self):
        self.registry['sections'][0]['publication_spec']['source_ids'].append('missing')
        with self.assertRaisesRegex(ValueError,'SECTION_REGISTERED_MEMBER_MISSING'):self.freeze()

    def test_unknown_additional_identity_still_blocks(self):
        data=self.add_untracked();data['nodes'][3]['id']='not-allowlisted';self.drive.content=self.repack(data)
        with self.assertRaisesRegex(ValueError,'SECTION_DISPLAY_IDENTITIES_INVALID'):self.freeze()

    def test_duplicate_viewer_identity_still_blocks(self):
        data=self.add_untracked();data['nodes'][3]['id']='source-one';self.drive.content=self.repack(data)
        with self.assertRaisesRegex(ValueError,'SECTION_DISPLAY_IDENTITIES_INVALID'):self.freeze()

    def test_partial_verified_member_wrong_parent_still_blocks(self):
        self.add_untracked();path=self.freeze();bundle=p._load(path/'bundle.json');data=v.unpack(self.drive.content.decode())
        next(i for i in bundle['workbook']['items'] if i['source_id']=='source-one')['observed']['parent_ids']=['wrong']
        with self.assertRaisesRegex(ValueError,'SECTION_MEMBER_NOT_VERIFIED'):v.integrate(data,bundle)

    def test_partial_counts_publish_identically_to_sheet_and_html(self):
        self.add_untracked();result=self.run_publish();self.assertEqual(result['result'],'PUBLICATION_VERIFIED')
        row=self.sheet.matrices['Migration Summary'][1]
        self.assertEqual(row[1:4],['PARTIAL',1,2])
        data=v.unpack(self.drive.content.decode());section=data['execution_sections']['sections'][0]
        self.assertEqual((section['status'],section['completed_file_count'],section['expected_file_count']),('PARTIAL',1,2))
