"""Policy-derived exclusions never become privacy grants or lose audit history."""
import copy
import unittest
from dataclasses import replace

from uqmc_recovery.agent_profile import register_agent_plan
from uqmc_recovery.model import SafetyError
from uqmc_recovery.publication_projection import _publication_exclusions
from uqmc_recovery.shortcut_policy import REASON, SHORTCUT_MIME, frozen_shortcut_exclusion_provenance
from test_projection import MemoryJournal
import test_agent_profile as registration
import test_retired_publication as publication


def policy(j,sid='untouched',**changes):
    return j.append('PLAN_EXCLUDED',payload={'file_id':sid,'reason_code':REASON,
        'mime_type':SHORTCUT_MIME,**changes})


def duplicate(j,sid='untouched',**changes):
    return j.append('EXCLUDED',payload={'file_id':sid,'reason_code':'FROZEN_PLAN_EXCLUSION',**changes})


class FrozenShortcutExclusionTests(unittest.TestCase):
    def test_registration_preserves_typed_policy_and_rejects_reentry(self):
        f=registration.AgentProfileTests();f.setUp();self.addCleanup(f.tearDown)
        original=policy(f.journal)
        plan=replace(registration.candidate(),excluded_ids=('untouched',))
        register_agent_plan(f.journal,plan);register_agent_plan(f.journal,plan)
        self.assertFalse(any(e['kind']=='EXCLUDED' for e in f.journal.records()))
        self.assertEqual(f.journal.records()[0],original)
        policy(f.journal,'file')
        with self.assertRaisesRegex(SafetyError,'EXCLUDED'):
            register_agent_plan(f.journal,replace(plan,run_id='next',batch_id='next',excluded_ids=()))

    def test_nonpolicy_frozen_exclusion_stays_private(self):
        f=registration.AgentProfileTests();f.setUp();self.addCleanup(f.tearDown)
        register_agent_plan(f.journal,replace(registration.candidate(),excluded_ids=('untouched',)))
        self.assertIn('untouched',_publication_exclusions(f.journal.records())[2])

    def test_historical_duplicate_is_provenance_linked_without_rewriting_events(self):
        f=publication.RetiredProjectionTests();f.setUp()
        original=policy(f.f.j);before=f.f.publish(sections=[f.spec])
        derived=duplicate(f.f.j);events=copy.deepcopy(f.f.j.events)
        links=frozen_shortcut_exclusion_provenance(events)
        self.assertEqual(links[derived['event_id']]['policy_event']['hash'],original['hash'])
        self.assertEqual(links[derived['event_id']]['derived_exclusion_event']['hash'],derived['hash'])
        after=f.f.publish(sections=[f.spec]);section=after['sections']['sections'][0]
        self.assertEqual((section['expected_file_count'],section['completed_file_count'],section['policy_excluded_file_count']),(1,1,1))
        self.assertIn('untouched',{i['source_id'] for i in after['html']['items']})
        for field in ('move','rename','observed'):
            self.assertEqual(f.f.item(after,'untouched')[field],f.f.item(before,'untouched')[field])
        self.assertEqual(f.f.j.events,events)
        self.assertNotIn('WITHDRAWN', f.f.item(after,'untouched')['reason_codes'])
        self.assertEqual(after['operation_log']['rows'],before['operation_log']['rows'])

    def test_independent_privacy_always_wins_before_or_after_policy(self):
        for order in ('before','after'):
            f=publication.RetiredProjectionTests();f.setUp()
            if order=='before':f.f.j.append('EXCLUDED',payload={'file_id':'untouched','reason_code':'PRIVATE_WITHDRAWAL'})
            policy(f.f.j);duplicate(f.f.j)
            if order=='after':f.f.j.append('EXCLUDED',payload={'file_id':'untouched','reason_code':'PRIVATE_WITHDRAWAL'})
            bundle=f.f.publish()
            self.assertNotIn('untouched',{i['source_id'] for i in bundle['html']['items']})
            self.assertIn('WITHDRAWN', f.f.item(bundle,'untouched')['reason_codes'])
            with self.assertRaisesRegex(ValueError,'SECTION_IDENTITIES_NOT_PUBLIC_ALLOWLISTED'):
                f.f.publish(sections=[f.spec])

    def test_unproved_duplicate_never_becomes_public(self):
        for mode in ('earlier','wrong-mime','wrong-reason','extra-fields'):
            with self.subTest(mode=mode):
                j=MemoryJournal()
                if mode=='earlier':duplicate(j)
                policy(j,**({'mime_type':'application/pdf'} if mode=='wrong-mime' else
                            {'reason_code':'UNVERIFIED_POLICY'} if mode=='wrong-reason' else {}))
                if mode!='earlier':duplicate(j,**({'private':True} if mode=='extra-fields' else {}))
                self.assertEqual(frozen_shortcut_exclusion_provenance(j.records()),{})
                self.assertIn('untouched',_publication_exclusions(j.records())[2])

    def test_proved_policy_never_expands_public_allowlist(self):
        f=publication.RetiredProjectionTests();f.setUp();policy(f.f.j);duplicate(f.f.j)
        f.f.allowed.remove('untouched')
        self.assertNotIn('untouched',{i['source_id'] for i in f.f.publish()['html']['items']})
        with self.assertRaisesRegex(ValueError,'SECTION_IDENTITIES_NOT_PUBLIC_ALLOWLISTED'):
            f.f.publish(sections=[f.spec])

    def test_automatic_publication_recovers_duplicate_without_migration_or_history_change(self):
        f=publication.RetiredAutomaticPublicationTests();f.setUp();self.addCleanup(f.doCleanups)
        policy(f.f.j,'untracked');duplicate(f.f.j,'untracked');events=copy.deepcopy(f.f.j.events)
        result=f.publish()
        self.assertEqual(result['result'],'PUBLICATION_VERIFIED',result)
        self.assertEqual(f.f.j.events,events)
        self.assertEqual(f.publish()['result'],'PUBLICATION_VERIFIED')
        self.assertEqual((len(f.f.drive.writes),len(f.f.sheet.updates)),(1,1))

if __name__=='__main__':unittest.main()
