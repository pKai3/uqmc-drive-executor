"""Offline evidence projection; no credentials, API calls, or real inventories."""
import copy
import json
import unittest
from test_projection import MemoryJournal
from uqmc_recovery.publication_projection import build_publication, FOLDER
from uqmc_recovery.projection import validate_snapshot


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.j=MemoryJournal(); self.ph='a'*64
        self.before={'id':'file','name':'old.pdf','parents':['old-parent'],'mime_type':'application/pdf'}
        self.target={**self.before,'name':'new.pdf','parents':['destination']}
        self.ops=[{'operation_id':'rename','file_id':'file','action':'RENAME','before':self.before,'target':{'name':'new.pdf'},'depends_on':[]},
                  {'operation_id':'move','file_id':'file','action':'MOVE','before':{**self.before,'name':'new.pdf'},'target':{'parents':['destination']},'depends_on':['rename']}]
        self.freeze(self.ops)
        self.base={'run_id':'run','plan_sha256':self.ph,'source_id':'file','direction':'FORWARD','operation_order':1,
                   'dependencies':[],'original_operation_ids':['rename','move'],'audit_permissions':False}
        self.allowed=['file','destination','untouched']
    def freeze(self,ops):
        self.j.append('DRAFTED',payload={'run_id':'run','batch_id':'run','proposal_hash':self.ph,
                      'plan_ref':self.j.evidence({'run_id':'run','operations':ops})})
    def desired(self,kind,**p):
        return self.j.append('DESIRED_'+kind,'desired:'+self.ph[:12]+':file:FORWARD',1,{**self.base,**p})
    def publish(self,**kw):
        return build_publication(self.j,allowed_source_ids=self.allowed,
            identity_basis={'identity_basis_version':1,'source_ids':self.allowed},**kw)
    def item(self,x,source='file'):
        return next(i for i in x['workbook']['items'] if i['source_id']==source)
    def applied(self,**overrides):
        self.desired('APPLIED',before_ref=self.j.evidence(self.before),target_ref=self.j.evidence(self.target),
                     observed_ref=self.j.evidence(self.target),args={'name':'new.pdf','addParents':'destination','removeParents':'old-parent'},**overrides)
    def test_combined_operation_and_public_redaction(self):
        self.applied(); x=self.publish(); i=self.item(x)
        self.assertEqual((i['rename']['effect'],i['move']['effect']),('APPLIED','APPLIED'))
        self.assertEqual(x['workbook']['operations'][0]['action'],'MOVE_AND_RENAME')
        self.assertIsNone(x['html']['items'][0]['observed']['exact_name'])
        self.assertNotIn('sha256:',json.dumps(x['html']))
        self.assertEqual(len(x['operation_log']['rows'][0]),26)
        validate_snapshot(x['workbook']);validate_snapshot(x['html'])
    def test_drafted_only_remains_untracked(self):
        x=self.publish(); self.assertEqual(x['workbook']['operations'],[])
        self.assertEqual(self.item(x)['rename']['effect'],'NOT_TRACKED')
        self.assertEqual(self.item(x,'untouched')['observed']['read_state'],'NEVER_VERIFIED')
    def test_legacy_rename_retained_when_desired_move_succeeds(self):
        self.j.append('RECONCILED_AT_TARGET','rename',1,{'run_id':'run','action':'RENAME','file_id':'file','direction':'FORWARD',
           'before_ref':self.j.evidence(self.before),'intended_ref':self.j.evidence({**self.before,'name':'new.pdf'}),
           'observed_ref':self.j.evidence({**self.before,'name':'new.pdf'})})
        self.desired('APPLIED',before_ref=self.j.evidence({**self.before,'name':'new.pdf'}),target_ref=self.j.evidence(self.target),
           observed_ref=self.j.evidence(self.target),args={'addParents':'destination','removeParents':'old-parent'})
        x=self.publish();i=self.item(x)
        self.assertEqual(i['rename']['effect'],'APPLIED');self.assertIn('rename',i['rename']['operation_ids'])
        self.assertEqual(len(x['operation_log']['rows']),2)
    def test_observed_target_resolves_current_state_without_erasing_old_attempt(self):
        self.desired('ATTEMPTED',before_ref=self.j.evidence(self.before),target_ref=self.j.evidence(self.target),args={'name':'new.pdf','addParents':'destination'})
        self.desired('STOPPED',state='UNCERTAIN')
        self.desired('OBSERVED',state='TARGET',observed_ref=self.j.evidence(self.target),request_outcome='READ_ONLY_CONFLICT_RECONCILIATION')
        x=self.publish();self.assertEqual(self.item(x)['move']['effect'],'APPLIED')
        self.assertIn('UNCERTAIN',[r[16] for r in x['operation_log']['rows']])
        self.assertEqual(x['workbook']['operations'][0]['reason_code'],'OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN')
    def test_verified_desired_target_clears_only_superseded_current_request_reasons(self):
        for reason in ('OBSERVED_TARGET_ATTRIBUTION_UNCERTAIN', 'REQUEST_NOT_PROVEN_TERMINAL'):
            self.j.append('UNCERTAIN','rename',1,{'run_id':'run','action':'RENAME','file_id':'file',
                'direction':'FORWARD','before_ref':self.j.evidence(self.before),
                'intended_ref':self.j.evidence(self.target),'reason_code':reason})
        before=self.publish()
        self.assertIn('REQUEST_NOT_PROVEN_TERMINAL',self.item(before)['reason_codes'])
        historical_rows=copy.deepcopy(before['operation_log']['rows'])
        historical_op=copy.deepcopy(before['workbook']['operations'][0])
        self.applied()
        after=self.publish()
        self.assertEqual(self.item(after)['reason_codes'],[])
        self.assertEqual(after['operation_log']['rows'][:2],historical_rows)
        self.assertEqual(after['workbook']['operations'][0],historical_op)
        self.assertNotEqual(before['workbook']['snapshot_id'],after['workbook']['snapshot_id'])

    def test_failed_desired_attempt_does_not_clear_unresolved_legacy_reason(self):
        self.j.append('UNCERTAIN','rename',1,{'run_id':'run','action':'RENAME','file_id':'file',
            'direction':'FORWARD','before_ref':self.j.evidence(self.before),
            'intended_ref':self.j.evidence(self.target),'reason_code':'REQUEST_NOT_PROVEN_TERMINAL'})
        self.desired('STOPPED',state='UNCERTAIN')
        self.assertIn('REQUEST_NOT_PROVEN_TERMINAL',self.item(self.publish())['reason_codes'])

    def test_rollback_restores_exact_original(self):
        self.applied()
        self.desired('ROLLED_BACK',direction='ROLLBACK',before_ref=self.j.evidence(self.target),target_ref=self.j.evidence(self.before),
                     observed_ref=self.j.evidence(self.before),args={'name':'old.pdf','addParents':'old-parent','removeParents':'destination'})
        i=self.item(self.publish());self.assertEqual(i['rename']['effect'],'REVERTED');self.assertEqual(i['move']['effect'],'REVERTED')
    def test_snapshot_id_deterministic_and_read_only(self):
        self.applied(); old=copy.deepcopy(self.j.events)
        a=self.publish();b=self.publish()
        self.assertEqual(a['html']['snapshot_id'],b['workbook']['snapshot_id'])
        self.assertEqual(self.j.events,old)
    def test_created_destination_overlay_and_completion(self):
        self.applied()
        authority={'decision':'APPROVED','folders':[{'key':'logical','name':'Destination'}]}
        raw={'id':'destination','name':'Destination','parents':['anchor'],'mime_type':FOLDER}
        self.j.append('REQUIRED_FOLDER_VERIFIED','create-op',payload={'section_id':'section','key':'logical','file_id':'destination',
            'name':'Destination','parent_id':'anchor','authority_ref':self.j.evidence(authority),'observed_ref':self.j.evidence(raw)})
        spec={'section_id':'section','title':'Destination','proposal_path':'Anchor/Destination','plan_sha256':self.ph,
              'source_ids':['file'],'destination_id':'destination','destination_mode':'CREATED'}
        x=self.publish(sections=[spec]);s=x['sections']['sections'][0]
        self.assertEqual(s['status'],'COMPLETED');self.assertEqual(s['completed_file_count'],1)
        self.assertEqual(s['created_folder_count'],1)
        self.assertEqual(x['operation_log']['rows'][-1][8],'CREATE_FOLDER')
        self.assertEqual(x['operation_log']['rows'][-1][5],1)
        self.assertEqual(x['sections']['snapshot_id'],x['html']['snapshot_id'])
        self.assertNotIn('sections',x['html'])
    def test_unverified_section_is_not_complete(self):
        spec={'section_id':'section','title':'Destination','proposal_path':'Anchor/Destination','plan_sha256':self.ph,
              'source_ids':['file'],'destination_id':'destination','destination_mode':'CREATED'}
        x=self.publish(sections=[spec]);self.assertEqual(x['sections']['sections'][0]['status'],'NOT_STARTED')
    def test_unlisted_or_excluded_section_identity_rejected(self):
        self.j.append('EXCLUDED',payload={'file_id':'file'})
        spec={'section_id':'section','title':'Destination','proposal_path':'Anchor/Destination','plan_sha256':self.ph,
              'source_ids':['file'],'destination_id':'destination','destination_mode':'CREATED'}
        with self.assertRaises(ValueError):self.publish(sections=[spec])

if __name__=='__main__':unittest.main()
