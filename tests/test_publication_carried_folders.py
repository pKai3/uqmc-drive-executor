"""Unchanged child-folder paths follow only exact verified ancestor revisions."""
import copy
import unittest
from test_viewer_revision import RevisionEvidenceTests
from uqmc_recovery.publication_projection import _carried_folder_observations, FOLDER


class CarriedFolderTests(unittest.TestCase):
    def make(self, *, verified=True, child_changes=None):
        f=RevisionEvidenceTests(methodName='runTest');f.make()
        event=f.j.events[0];e=f.j.read_evidence(event['payload']['manifest_ref'])
        raw=f.j.read_evidence(e['source_metadata_ref'])
        raw['metadata']['child']={'id':'child','name':'Attachments','parents':['folder'],
            'mimeType':FOLDER,'trashed':False,**(child_changes or {})}
        e['source_metadata_ref']=f.j.evidence(raw)
        e['prior_destination_paths']['Root/General/2024/Attachments']='child'
        e['destination_paths']['Root/2024/Minutes/Attachments']='child'
        event['payload']['manifest_ref']=f.j.evidence(e)
        if verified:f.observe()
        observed={('section','child'):{'id':'child','name':'Attachments','parent_ids':['folder'],
            'path':'Root/General/2024/Attachments','verified_at_utc':'2026-01-01T00:00:00Z'}}
        return f,observed

    def test_verified_ancestor_updates_path_only_without_journal_or_timestamp_change(self):
        f,before=self.make();events=f.j.records();original=copy.deepcopy(before)
        after=_carried_folder_observations(f.j,before)
        self.assertEqual(after[('section','child')],{**original[('section','child')],
            'path':'Root/2024/Minutes/Attachments'})
        self.assertEqual(before,original);self.assertEqual(f.j.records(),events)

    def test_unverified_ancestor_and_actual_child_drift_do_not_rebase(self):
        for changes in ({'verified':False},{'child_changes':{'name':'Human edit'}},
                        {'child_changes':{'parents':['other']}},{'child_changes':{'trashed':True}},
                        {'child_changes':{'mimeType':'application/pdf'}}):
            with self.subTest(changes=changes):
                f,before=self.make(**changes)
                self.assertEqual(_carried_folder_observations(f.j,before),before)

    def test_later_child_conflict_uncertainty_or_attempt_keeps_prior_observation(self):
        for kind,payload in [('DESIRED_STOPPED',{'state':'CONFLICT'}),
                             ('DESIRED_STOPPED',{'state':'UNCERTAIN'}),('DESIRED_ATTEMPTED',{})]:
            with self.subTest(kind=kind,payload=payload):
                f,before=self.make()
                f.j.append(kind,payload={'source_id':'child',**payload})
                self.assertEqual(_carried_folder_observations(f.j,before),before)


if __name__=='__main__':unittest.main()
