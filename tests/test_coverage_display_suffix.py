"""Keep-name display suffixes require the same ID's exact live title and MIME."""
import unittest
from uqmc_recovery.importer import _subtree_coverage_records
from uqmc_recovery.destination_planner import file_parent_path
from uqmc_recovery.model import SafetyError

class CoverageDisplaySuffixTests(unittest.TestCase):
    def record(self, title, suffix, mime, target_title=None):
        root={'action':'MOVE','source_id':'folder','reference':'MOVE/ITEM',
              'proposal_refs':['MOVE/ITEM'],'decision':'YES','withdrawn':False,'row_sha256':'root',
              'row':{'Scope':'Whole subtree','Exact target path':'Archive/COVID',
                     'Proposal ID':'MOVE','Manifest item':'ITEM'}}
        display=title+suffix
        row={'Source ID':'document','Assessment':'Keep name','Move proposal':'MOVE',
             'Existing proposed target':'Archive/COVID/Old/'+(target_title or title),
             'Current name + type suffix':display,'Proposed name / retained name':display,
             'Verified MIME type':mime,'Rename proposal':''}
        return next(iter(_subtree_coverage_records({'MOVE/ITEM':root},[row],'Archive').values()))

    def test_uploaded_display_suffix_and_literal_slashes_require_exact_live_title(self):
        for suffix,mime in [('.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                            ('.PDF','application/pdf'),('.png','image/png')]:
            title='Approved on 12/04/2021 - Plan'
            r=self.record(title,suffix,mime)
            state={'id':'document','name':title,'mime_type':mime}
            self.assertEqual(file_parent_path(r,None,state),'Archive/COVID/Old')
            self.assertEqual(r['row']['Current item'],title+suffix)
            for changed in [dict(state,name=title+suffix),dict(state,mime_type='text/plain')]:
                with self.assertRaisesRegex(SafetyError,'KEEP_NAME_COVERAGE_METADATA_CONFLICT'):
                    file_parent_path(r,None,changed)

    def test_true_extension_is_preserved_when_present_in_target(self):
        r=self.record('File','.pdf','application/pdf',target_title='File.pdf')
        self.assertNotIn('coverage_exact_name',r)
        self.assertEqual(file_parent_path(r,None,{'id':'document','name':'File.pdf','mime_type':'application/pdf'}),'Archive/COVID/Old')

    def test_unrelated_suffix_or_title_is_not_accepted(self):
        for title,suffix,mime,target in [('File','.docx','application/pdf','File'),
                                        ('File','.pdf','application/pdf','Different')]:
            with self.assertRaisesRegex(SafetyError,'KEEP_NAME_COVERAGE_PATH_CONFLICT'):
                self.record(title,suffix,mime,target)
