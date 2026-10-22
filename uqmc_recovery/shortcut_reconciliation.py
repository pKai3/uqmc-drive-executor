"""Explicit local reconciliation of a successful shortcut move's owner change.

Never dispatches a Drive write. The normal runner's ownership guard stays in
force. An operator must supply private, exact-attempt human authorization and
the successful response must already prove the target placement and new owner.
"""
import copy

from .desired import core
from .model import SafetyError


SHORTCUT = 'application/vnd.google-apps.shortcut'
PROTECTED = ('id', 'mimeType', 'size', 'md5Checksum', 'sha1Checksum',
             'sha256Checksum', 'owners', 'driveId', 'shortcutDetails', 'trashed')


def reconcile_shortcut_owner_change(runner, source_id, *, authorization_ref):
    """Accept the recorded owner side effect, keeping the original rollback image.

    The one APPLIED event atomically closes the existing request and contains
    the explicit reconciliation, raw current observation and unchanged original
    evidence refs. Retrying after a crash either repeats a read or returns that
    same event; it cannot issue a move or a permission/ownership request.
    """
    with runner.journal.locked():
        item = runner.by_id[source_id]
        auth = runner.journal.read_evidence(authorization_ref)
        if (auth.get('schema_version') != 'uqmc.shortcut-owner-reconciliation.v1'
                or auth.get('authority') != 'human_user'
                or auth.get('accepted') is not True
                or auth.get('source_id') != source_id
                or auth.get('plan_sha256') != runner.plan_sha256
                or not auth.get('instruction')):
            raise SafetyError('EXPLICIT_SHORTCUT_RECONCILIATION_AUTHORITY_REQUIRED')
        completed = [e for e in runner.events(source_id)
                     if e['kind'] == 'DESIRED_APPLIED'
                     and e['payload'].get('reconciliation_authorization_ref') == authorization_ref]
        if completed:
            return completed[-1]
        runner.authority()
        pending = runner.pending(item, 'FORWARD')
        if not pending:
            raise SafetyError('SHORTCUT_RECONCILIATION_PENDING_MOVE_REQUIRED')
        p = pending['payload']
        receipts = [e for e in runner.events(source_id)
                    if e['seq'] > pending['seq'] and e['kind'] == 'DESIRED_RECEIPT'
                    and e['payload'].get('attempt_id') == p['attempt_id']
                    and e['payload'].get('request_outcome') == 'RETURNED_SUCCESS']
        if len(receipts) != 1:
            raise SafetyError('SHORTCUT_RECONCILIATION_SUCCESS_RECEIPT_REQUIRED')
        receipt = receipts[0]['payload']
        binding = {'attempt_id': p['attempt_id'], 'before_ref': p['before_ref'],
                   'target_ref': p['target_ref'], 'response_ref': receipt['response_ref']}
        if any(auth.get(k) != v for k, v in binding.items()):
            raise SafetyError('SHORTCUT_RECONCILIATION_ATTEMPT_BINDING_MISMATCH')
        before = runner.journal.read_evidence(p['before_ref'])
        target = runner.journal.read_evidence(p['target_ref'])
        response = runner.journal.read_evidence(receipt['response_ref'])
        if (before.get('mimeType') != SHORTCUT or target != item['target']
                or target['name'] != before['name'] or target['parents'] == core(before)['parents']
                or not before.get('shortcutDetails', {}).get('targetId')
                or core(response) != target
                or p.get('args') != runner.delta(before, target)
                or receipt.get('args') != p['args']
                or not before.get('owners') or not response.get('owners')):
            raise SafetyError('SHORTCUT_RECONCILIATION_EXACT_MOVE_REQUIRED')
        changed = [k for k in PROTECTED if before.get(k) != response.get(k)]
        if changed != ['owners']:
            raise SafetyError('SHORTCUT_RECONCILIATION_OTHER_PROTECTED_DRIFT')
        observed = runner.read(source_id)
        # Bind acceptance to the actual successful response, not an arbitrary
        # later state. Capability changes are retained, never globally ignored.
        fields = (*PROTECTED, 'name', 'parents', 'capabilities', 'version', 'modifiedTime')
        if any(response.get(k) != observed.get(k) for k in fields):
            raise SafetyError('SHORTCUT_RECONCILIATION_CURRENT_STATE_CHANGED')
        runner.authority()
        observed_ref = runner.journal.evidence(observed)
        differences = {k: {'before': copy.deepcopy(before.get(k)),
                           'observed': copy.deepcopy(observed.get(k))}
                       for k in fields if before.get(k) != observed.get(k)}
        payload = {k: v for k, v in p.items() if k not in {
            'plan_sha256', 'run_id', 'source_id', 'direction', 'operation_order',
            'dependencies', 'original_operation_ids', 'historical_uncertain_operation_ids',
            'audit_permissions'}}
        return runner.record('APPLIED', item, attempt=pending['attempt'], **payload,
            applied_ref=observed_ref, observed_ref=observed_ref,
            request_outcome='RETURNED_SUCCESS',
            response_ref=receipt['response_ref'],
            reconciliation_kind='EXPLICIT_SHORTCUT_OWNER_CHANGE_ACCEPTED',
            reconciliation_authorization_ref=authorization_ref,
            reconciled_attempt_event_id=pending['event_id'],
            metadata_differences=differences,
            ownership_restoration_guaranteed=False)
