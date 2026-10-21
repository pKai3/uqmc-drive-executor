"""Fresh-sheet candidate handoff and a fail-closed gate for new CLI batches.

Capture/freeze/bind are read-only toward Google. Binding is not an approval.
An already started exact plan can resume its immutable history; it cannot adopt
new sheet cells until explicitly paused, refreshed and bound as a new batch.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .__main__ import plan_from_json, save_new
from .journal import Journal
from .model import SafetyError, digest


def _core(value):
    from .desired import core
    return core(value)


def _age(snapshot):
    try:
        stamp = datetime.fromisoformat(snapshot['captured_at_utc'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
        seconds = (datetime.now(timezone.utc) - stamp).total_seconds()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError('FRESH_BATCH_CAPTURE_TIME_REQUIRED') from None
    if seconds < 0 or seconds > 300:
        raise SafetyError('FRESH_BATCH_CAPTURE_EXPIRED_REFRESH_AGAIN')


def _snapshot_for_plan(journal, plan, snapshot_ref, *, fresh):
    snapshot = journal.read_evidence(snapshot_ref)
    records = journal.records()
    if (snapshot.get('schema_version') != 'uqmc.batch-refresh.candidates.v1' or
            snapshot.get('batch_id') != plan.batch_id or snapshot.get('execution_approved') is not False):
        raise SafetyError('FRESH_BATCH_SNAPSHOT_MISMATCH')
    matches = [e for e in records if e['kind'] == 'BATCH_REFRESHED'
        and e['payload'].get('snapshot_ref') == snapshot_ref
        and e['payload'].get('snapshot_sha256') == digest(snapshot)]
    if not matches:
        raise SafetyError('RECORDED_LIVE_BATCH_REFRESH_REQUIRED')
    if fresh:
        _age(snapshot)
    capture = journal.read_evidence(snapshot['capture_ref'])
    if (capture.get('capture_sha256') != snapshot.get('capture_sha256') or
            capture.get('source_revision') != snapshot.get('source_revision') or
            capture.get('workbook_id') != snapshot.get('workbook_id')):
        raise SafetyError('FRESH_BATCH_CAPTURE_BINDING_MISMATCH')
    candidates = {i['source_id']: i for i in snapshot['pending_candidates']}
    if len(candidates) != len(snapshot['pending_candidates']):
        raise SafetyError('FRESH_BATCH_DUPLICATE_SOURCE')
    states, actions = {}, {}
    for op in plan.ordered():
        candidate = candidates.get(op.file_id)
        if candidate is None or op.direction != 'FORWARD' or op.action not in {'MOVE','RENAME'}:
            raise SafetyError('PLAN_OUTSIDE_FRESH_PENDING_CANDIDATES')
        before = states.setdefault(op.file_id, candidate['before'])
        if _core(op.before) != before:
            raise SafetyError('FRESH_BATCH_PREIMAGE_MISMATCH')
        if op.action in actions.setdefault(op.file_id, set()):
            raise SafetyError('FRESH_BATCH_REPEATED_ACTION')
        actions[op.file_id].add(op.action)
        states[op.file_id] = _core(op.intended())
    if not states:
        raise SafetyError('FRESH_BATCH_HAS_NO_PENDING_OPERATIONS')
    for source, state in states.items():
        if state != candidates[source]['target'] or actions[source] != set(candidates[source]['actions']):
            raise SafetyError('PLAN_TARGET_DIFFERS_FROM_FRESH_WORKBOOK')
    sequence = snapshot['history_sequence']
    if (type(sequence) is not int or sequence < 0 or sequence > len(records) or
            (records[sequence-1]['hash'] if sequence else None) != snapshot['history_tip_sha256']):
        raise SafetyError('FRESH_BATCH_HISTORY_MISMATCH')
    if fresh:
        for event in records[sequence:]:
            p = event['payload']
            if (p.get('source_id', p.get('file_id')) in states and event['kind'] in {
                'ATTEMPTED','UNCERTAIN','VERIFIED_APPLIED','RECONCILED_AT_TARGET',
                'DESIRED_ATTEMPTED','DESIRED_APPLIED','DESIRED_ROLLED_BACK','DESIRED_OBSERVED'}):
                raise SafetyError('HISTORY_CHANGED_SINCE_BATCH_REFRESH')
    return snapshot


def refreshed_section_authority(journal, plan):
    """Retain revocation/expiry checks on the originating section authority."""
    from .section_importer import validate_section_authority
    records = journal.records()
    links = [r for r in records if r['kind']=='BATCH_SOURCE_BOUND'
             and r['payload'].get('proposal_hash')==plan.proposal_hash]
    if not links:
        raise SafetyError('FRESH_BATCH_SOURCE_BINDING_REQUIRED')
    snapshot = _snapshot_for_plan(journal, plan, links[-1]['payload']['snapshot_ref'], fresh=False)
    drafts = [r for r in records if r['kind']=='DRAFTED'
              and r['payload'].get('proposal_hash')==snapshot['existing_plan_sha256']]
    if not drafts or snapshot['existing_plan_sha256']==plan.proposal_hash:
        raise SafetyError('FRESH_BATCH_PRIOR_PLAN_REQUIRED')
    prior = plan_from_json(journal.read_evidence(drafts[-1]['payload']['plan_ref']))
    authority = validate_section_authority(journal, prior)
    if not authority or authority['section_id']!=snapshot['section_id']:
        raise SafetyError('FRESH_BATCH_SECTION_AUTHORITY_MISMATCH')
    return authority | {'proposal_hash':plan.proposal_hash,'proposal_revision':plan.proposal_revision,
        'refresh_snapshot_ref':links[-1]['payload']['snapshot_ref']}


def bind_batch_source(journal, plan, snapshot_ref):
    """Bind an already prepared exact plan to fresh pending candidates, not approval."""
    from .agent_profile import register_agent_plan
    with journal.locked():
        snapshot = _snapshot_for_plan(journal, plan, snapshot_ref, fresh=True)
        if plan.proposal_hash==snapshot['existing_plan_sha256']:
            raise SafetyError('NEW_BATCH_REQUIRES_NEW_FROZEN_PLAN')
        if any(e['kind']=='BATCH_SOURCE_BOUND' and e['payload'].get('proposal_hash')==plan.proposal_hash
               for e in journal.records()):
            raise SafetyError('BATCH_SOURCE_ALREADY_BOUND')
        # Existing validation checks actual preimage structure, names/extensions,
        # dependency ordering and excluded identities. It grants no authority.
        register_agent_plan(journal, plan)
        return journal.append('BATCH_SOURCE_BOUND',payload={'proposal_hash':plan.proposal_hash,
            'run_id':plan.run_id,'batch_id':plan.batch_id,'snapshot_ref':snapshot_ref,
            'input_sha256':snapshot['input_sha256'],'execution_approved':False})


def require_batch_source(journal, plan, *, resume=False):
    with journal.locked():
        records = journal.records()
        for event in records:
            if event['kind']=='BATCH_REFRESH_REQUIRED' and event['payload'].get('proposal_hash')==plan.proposal_hash:
                raise SafetyError('PLAN_EDITS_REQUIRE_NEW_REFRESHED_BATCH')
        started = any(e['kind'] in {'ATTEMPTED','DESIRED_ATTEMPTED'} and
            e['payload'].get('plan_sha256',e['payload'].get('proposal_hash'))==plan.proposal_hash for e in records)
        if resume and started:
            return  # Same frozen run only; actual effect checks still skip success.
        links=[e for e in records if e['kind']=='BATCH_SOURCE_BOUND' and e['payload'].get('proposal_hash')==plan.proposal_hash]
        if not links:
            raise SafetyError('NEW_BATCH_REQUIRES_FRESH_WORKBOOK_CAPTURE_AND_BINDING')
        _snapshot_for_plan(journal,plan,links[-1]['payload']['snapshot_ref'],fresh=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('refresh','bind','invalidate'))
    parser.add_argument('--store',required=True)
    parser.add_argument('--plan',required=True)
    parser.add_argument('--policy')
    parser.add_argument('--token')
    parser.add_argument('--batch-id')
    parser.add_argument('--snapshot-ref')
    parser.add_argument('--output')
    parser.add_argument('--reason')
    args=parser.parse_args(argv)
    journal=Journal(args.store)
    plan=plan_from_json(json.loads(Path(args.plan).read_text()))
    try:
        if args.command=='refresh':
            if not all((args.policy,args.token,args.batch_id,args.output)):
                raise SafetyError('REFRESH_REQUIRES_POLICY_TOKEN_BATCH_OUTPUT')
            from .sheets_capture import GoogleSheetsCapture
            from .batch_refresh import refresh_batch
            policy=json.loads(Path(args.policy).read_text())
            collector=GoogleSheetsCapture.from_authorized_user_file(args.token,workbook_id=policy['workbook_id'])
            result=refresh_batch(journal,plan,collector.capture,policy,batch_id=args.batch_id)
            output=save_new(args.output,result['snapshot'])
            print(json.dumps({'evidence_ref':result['evidence_ref'],'output':output,
                'pending':len(result['snapshot']['pending_candidates']),
                'blocked':len(result['snapshot']['blocked']),'execution_approved':False}))
        elif args.command=='bind':
            if not args.snapshot_ref:
                raise SafetyError('SNAPSHOT_REFERENCE_REQUIRED')
            event=bind_batch_source(journal,plan,args.snapshot_ref)
            print(json.dumps({'sequence':event['seq'],'bound_plan_sha256':plan.proposal_hash,'execution_approved':False}))
        else:
            if not args.reason:
                raise SafetyError('REFRESH_REASON_REQUIRED')
            event=journal.append('BATCH_REFRESH_REQUIRED',payload={'proposal_hash':plan.proposal_hash,'reason':args.reason})
            print(json.dumps({'sequence':event['seq'],'resume_blocked':True}))
        return 0
    except Exception as exc:
        from .direct_transport import TransportError
        print(json.dumps({'error':str(exc) if isinstance(exc,SafetyError) else type(exc).__name__,
            **({'request_error':exc.as_dict()} if isinstance(exc,TransportError) else {})}))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
