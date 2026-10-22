"""Native display suffix handling keeps exact historical titles and drift checks."""
import unittest

import test_destination_command as fixtures
from test_importer import append_row
from uqmc_recovery.importer import HEADERS
from uqmc_recovery.journal import Journal


class NativeDisplayTitleTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DestinationCommandTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def native(self,mime,current,display,target):
        item=self.f.server.drive.files['leaf'];item['mimeType']=mime;item['name']=current
        item.pop('size',None);item.pop('md5Checksum',None)
        row=self.f.export['sheets']['Proposed Renames'][2];headers=HEADERS['Proposed Renames']
        row[headers.index('Current name + type suffix')]=display
        row[headers.index('Proposed name + type suffix')]=target

    def move(self,current,source_path):
        append_row(self.f.export,'Proposed Moves',{'Proposal ID':'MOVE-LEAF','Action':'Move','Proposed target path':fixtures.SECTION})
        append_row(self.f.export,'Move Manifest',{'Proposal ID':'MOVE-LEAF','Manifest item':'ITEM-LEAF',
            'Source ID':'leaf','Current item':current,'Source path':source_path,
            'Exact target path':fixtures.SECTION+'/'+current,'Scope':'Single item'})

    def test_google_form_display_suffix_is_not_written_to_native_title(self):
        self.native('application/vnd.google-apps.form','Original form','Original form.gform','Approved form.gform')
        result=self.f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(self.f.server.drive.files['leaf']['name'],'Approved form')
        self.assertEqual(self.f.server.requests[-1],{'fileId':'leaf','name':'Approved form'})

    def test_literal_native_doc_suffix_in_real_original_name_is_preserved_as_preimage(self):
        self.native('application/vnd.google-apps.document','Trip Proposal.gdoc','Trip Proposal.gdoc','Updated proposal.gdoc')
        result=self.f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(self.f.server.drive.files['leaf']['name'],'Updated proposal')
        self.assertEqual(self.f.server.requests[-1],{'fileId':'leaf','name':'Updated proposal'})

    def test_trailing_space_original_requires_exact_manifest_and_source_path(self):
        self.native('application/vnd.google-apps.form','Release form ','Release form.gform','Updated release.gform')
        self.move('Release form ','Old parent/Old folder/Release form ')
        result=self.f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2},result)
        self.assertEqual(self.f.server.drive.files['leaf']['name'],'Updated release')
        self.assertEqual(self.f.server.created,[])

    def test_trailing_space_without_source_path_proof_is_explicit_warning(self):
        self.native('application/vnd.google-apps.form','Release form ','Release form.gform','Updated release.gform')
        self.move('Release form ','Old parent/Old folder/Release form')
        result=self.f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2})
        warning=next(e for e in Journal(self.f.config['store']).records() if e['kind']=='SOURCE_NAME_DRIFT_ACCEPTED')
        self.assertEqual(warning['payload']['observed_name'],'Release form ')
        self.assertEqual(self.f.server.drive.files['leaf']['name'],'Updated release')

    def test_genuine_native_name_change_is_recorded_not_normalized_away(self):
        self.native('application/vnd.google-apps.form','Human changed form','Original form.gform','Approved form.gform')
        result=self.f.run_command(apply=True)
        self.assertEqual(result['counts'],{'APPLIED':2})
        warning=next(e for e in Journal(self.f.config['store']).records() if e['kind']=='SOURCE_NAME_DRIFT_ACCEPTED')
        self.assertEqual(warning['payload']['original_name'],'Original form')
        self.assertEqual(warning['payload']['observed_name'],'Human changed form')
        self.assertEqual(self.f.server.drive.files['leaf']['name'],'Approved form')


if __name__=='__main__':unittest.main()
