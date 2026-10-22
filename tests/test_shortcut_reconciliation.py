"""Exact-attempt shortcut reconciliation; fake Drive only."""
import copy
import unittest
from unittest.mock import patch

import test_desired as fixtures
from uqmc_recovery.destination_history import build_history_index, reconcile_item
from uqmc_recovery.model import Operation, SafetyError
from uqmc_recovery.shortcut_reconciliation import reconcile_shortcut_owner_change


class ShortcutReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DesiredStateTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.files['fixture-shortcut'] = self.f.files.pop('fixture-source')
        raw = self.f.files['fixture-shortcut']
        raw['id'] = 'fixture-shortcut'
        raw.update(name='Shortcut', mimeType='application/vnd.google-apps.shortcut',
                   owners=[{'emailAddress': 'old@example.invalid'}],
                   shortcutDetails={'targetId': 'fixture-linked-folder',
                                    'targetMimeType': 'application/vnd.google-apps.folder'},
                   capabilities={'canMoveItemWithinDrive': True, 'canMoveItemOutOfDrive': False})
        op = Operation('fixture-shortcut-move', 'fixture-shortcut', 'MOVE',
                       self.f.snapshot('fixture-shortcut'), {'parents': ['fixture-destination']})
        self.f.freeze((op,)); self.r = self.f.runner(audit_permissions=False)
        def changed_owner(item):
            item['owners'] = [{'emailAddress': 'new@example.invalid'}]
            item['capabilities']['canMoveItemOutOfDrive'] = True
        self.f.transport.after_update = changed_owner
        result = self.f.run_plan(self.r)
        self.assertEqual(result['counts'], {'CONFLICT': 1})
        self.assertEqual(result['items'][0]['reason'], 'PROTECTED_METADATA_DRIFT')
        events = self.r.events('fixture-shortcut')
        self.attempt = next(e for e in events if e['kind'] == 'DESIRED_ATTEMPTED')
        receipt = next(e for e in events if e['kind'] == 'DESIRED_RECEIPT')['payload']
        self.auth = {'schema_version': 'uqmc.shortcut-owner-reconciliation.v1',
            'authority': 'human_user', 'accepted': True, 'source_id': 'fixture-shortcut',
            'plan_sha256': self.r.plan_sha256, 'instruction': 'Accept this recorded shortcut owner change; no replay.',
            **{k: self.attempt['payload'][k] for k in ('attempt_id', 'before_ref', 'target_ref')},
            'response_ref': receipt['response_ref']}
        self.f.transport.after_update = None

    def reconcile(self, auth=None):
        ref = self.f.journal.evidence(self.auth if auth is None else auth)
        return reconcile_shortcut_owner_change(self.r, 'fixture-shortcut', authorization_ref=ref)

    def test_acceptance_preserves_history_skips_replay_and_keeps_rollback_name_parent(self):
        old = self.f.journal.records()
        event = self.reconcile()
        self.assertEqual(self.f.journal.records()[:len(old)], old)
        self.assertEqual(event['kind'], 'DESIRED_APPLIED')
        self.assertEqual(event['payload']['before_ref'], self.auth['before_ref'])
        before = self.f.journal.read_evidence(event['payload']['before_ref'])
        self.assertEqual(before['owners'], [{'emailAddress': 'old@example.invalid'}])
        self.assertEqual(event['payload']['metadata_differences']['owners']['observed'],
                         [{'emailAddress': 'new@example.invalid'}])
        self.assertIsNone(self.r.pending(self.r.items[0], 'FORWARD'))
        self.assertEqual(self.reconcile()['event_id'], event['event_id'])
        self.assertEqual(self.f.run_plan(self.r)['counts'], {'TARGET': 1})
        history = build_history_index(self.f.journal)['fixture-shortcut']
        self.assertEqual(history['pending_requests'], [])
        result = reconcile_item(history, self.r.items[0]['target'], self.f.transport.get_file('fixture-shortcut'))
        self.assertEqual(result['disposition'], 'COMPLETE')
        self.assertEqual(len(self.f.transport.calls), 1)
        self.assertEqual(self.f.run_plan(self.r, rollback=True)['counts'], {'ROLLED_BACK': 1})
        self.assertEqual(self.f.transport.files['fixture-shortcut']['parents'], ['fixture-parent'])
        self.assertEqual(self.f.transport.files['fixture-shortcut']['owners'], [{'emailAddress': 'new@example.invalid'}])
        self.assertEqual(self.f.transport.permission_reads, {})

    def test_normal_resume_still_blocks_without_explicit_acceptance(self):
        self.assertEqual(self.f.run_plan(self.r)['counts'], {'CONFLICT': 1})
        self.assertEqual(len(self.f.transport.calls), 1)
        for field,value in [('accepted', False), ('attempt_id', 'other-attempt'),
                            ('source_id', 'other-source'), ('response_ref', self.auth['before_ref'])]:
            with self.subTest(field=field), self.assertRaises(SafetyError):
                self.reconcile({**self.auth, field:value})

    def test_current_target_owner_and_capability_drift_are_rejected(self):
        original = copy.deepcopy(self.f.transport.files['fixture-shortcut'])
        for field,value in [('parents',['fixture-parent']), ('name','Human edit'),
                            ('shortcutDetails',{'targetId':'different-target'}),
                            ('owners',[{'emailAddress':'third@example.invalid'}]),
                            ('capabilities',{'canMoveItemWithinDrive':False})]:
            with self.subTest(field=field):
                self.f.transport.files['fixture-shortcut'] = {**copy.deepcopy(original),field:value}
                with self.assertRaisesRegex(SafetyError,'CURRENT_STATE_CHANGED'):
                    self.reconcile()
        self.assertEqual(len(self.f.transport.calls),1)

    def test_lost_response_cannot_be_reclassified_as_known_success(self):
        # The authorization cannot substitute for a matching successful receipt.
        events = [e for e in self.r.events('fixture-shortcut') if e['kind'] != 'DESIRED_RECEIPT']
        with patch.object(self.r, 'events', return_value=events):
            with self.assertRaisesRegex(SafetyError,'SUCCESS_RECEIPT_REQUIRED'):
                self.reconcile()
        self.assertEqual(len(self.f.transport.calls),1)

    def test_interrupted_local_reconciliation_retries_reads_without_mutation(self):
        with patch.object(self.r, 'record', side_effect=fixtures.ProcessDeath):
            with self.assertRaises(fixtures.ProcessDeath):
                self.reconcile()
        self.assertIsNotNone(self.r.pending(self.r.items[0], 'FORWARD'))
        self.r = self.f.runner(audit_permissions=False)
        event = self.reconcile()
        self.assertEqual(event['kind'], 'DESIRED_APPLIED')
        self.assertEqual(len(self.f.transport.calls), 1)


if __name__ == '__main__':
    unittest.main()
