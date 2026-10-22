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


def _initial_capture_freshness(capture, revalidation=None):
    """A fresh unchanged revision proves old captured cells remain current.

    Keep the original capture timestamp/hash. The separate read bracket has its
    own five-minute lifetime, measured from request start, not response arrival.
    """
    if revalidation is None:
        return _age({'captured_at_utc': capture.get('exported_at_utc')})
    if (not isinstance(revalidation, dict)
            or revalidation.get('method') != 'drive.files.get_revision'
            or revalidation.get('capture_sha256') != capture.get('capture_sha256')
            or revalidation.get('source_revision') != capture.get('source_revision')):
        raise SafetyError('INITIAL_BATCH_REVISION_REVALIDATION_MISMATCH')
    try:
        stamps = [datetime.fromisoformat(value.replace('Z', '+00:00')) for value in (
            capture['exported_at_utc'], revalidation['started_at_utc'], revalidation['checked_at_utc'])]
        if any(stamp.tzinfo is None for stamp in stamps) or not stamps[0] <= stamps[1] <= stamps[2]:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError('INITIAL_BATCH_REVISION_REVALIDATION_TIME_INVALID') from None
    _age({'captured_at_utc': revalidation['started_at_utc']})
    _age({'captured_at_utc': revalidation['checked_at_utc']})


def revalidate_initial_capture(capture, revision_reader):
    """Read the current workbook revision; never restamp captured sheet cells."""
    started = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    revision = revision_reader()
    checked = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    if revision != capture['source_revision']:
        raise SafetyError('WORKBOOK_CHANGED_BEFORE_BIND_REFRESH_REQUIRED')
    proof = {'method': 'drive.files.get_revision',
        'capture_sha256': capture['capture_sha256'], 'source_revision': revision,
        'started_at_utc': started, 'checked_at_utc': checked}
    _initial_capture_freshness(capture, proof)
    return proof


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


def _initial_source(journal, plan, capture, *, fresh, revalidation=None):
    """Prove this initial imported plan used this exact canonical capture."""
    from .agent_profile import _require_frozen_agent
    from .importer import catalog
    from .section_importer import validate_section_authority
    records = journal.records()
    _require_frozen_agent(journal, plan, records)
    authority = validate_section_authority(journal, plan)
    if not authority or authority.get('refresh_snapshot_ref'):
        raise SafetyError('INITIAL_BATCH_SECTION_IMPORT_REQUIRED')
    origin = journal.read_evidence(authority['section_manifest_ref'])
    workbook_id = origin['manifest']['workbook_id']
    projection = origin['projection']
    revision = capture.get('source_revision', {})
    if (capture.get('workbook_id') != workbook_id or
            capture.get('capture_method') != 'sheets.spreadsheets.get_with_drive_revision_bracket' or
            not isinstance(revision, dict) or revision.get('id') != workbook_id or
            revision.get('mimeType') != 'application/vnd.google-apps.spreadsheet' or
            revision.get('trashed') is not False or
            not isinstance(revision.get('version'), str) or not revision['version'].isdecimal() or
            not isinstance(revision.get('modifiedTime'), str) or not revision['modifiedTime']):
        raise SafetyError('INITIAL_BATCH_LIVE_CAPTURE_REQUIRED')
    try:
        capture_hash = digest({key: capture[key] for key in
            ('workbook_id', 'sheets', 'complete', 'cell_evidence', 'source_revision')})
    except KeyError:
        raise SafetyError('INITIAL_BATCH_LIVE_CAPTURE_REQUIRED') from None
    if capture.get('capture_sha256') != capture_hash:
        raise SafetyError('INITIAL_BATCH_CAPTURE_HASH_MISMATCH')
    source = catalog(capture, expected_workbook_id=workbook_id)
    if (source['input_sha256'] != capture.get('input_sha256') or
            source['input_sha256'] != projection.get('input_sha256')):
        raise SafetyError('INITIAL_BATCH_WORKBOOK_DIFFERS_FROM_SECTION_IMPORT')
    original = journal.read_evidence(origin['workbook_import_ref'])['workbook_export']
    if (original.get('capture_sha256') != capture_hash or
            original.get('source_revision') != revision or
            original.get('exported_at_utc') != capture.get('exported_at_utc')):
        raise SafetyError('INITIAL_BATCH_CAPTURE_DIFFERS_FROM_SECTION_IMPORT')
    if fresh:
        _initial_capture_freshness(capture, revalidation)
    return authority


def bind_initial_section_source(journal, plan, capture, *, revision_reader=None):
    """Bind an initial section import to its one fresh capture; never approve it.

    The caller imports and registers the exact plan first. A later capture or
    edited plan requires ordinary refresh/reconciliation, not this shortcut.
    """
    with journal.locked():
        authority = _initial_source(journal, plan, capture, fresh=False)
        records = journal.records()
        if any(e['kind'] in {'INITIAL_BATCH_SOURCE_BOUND', 'BATCH_SOURCE_BOUND'} and
                e['payload'].get('proposal_hash') == plan.proposal_hash for e in records):
            raise SafetyError('BATCH_SOURCE_ALREADY_BOUND')
        if any(e['kind'] in {'ATTEMPTED', 'DESIRED_ATTEMPTED'} and
                e['payload'].get('plan_sha256', e['payload'].get('proposal_hash')) == plan.proposal_hash
                for e in records):
            raise SafetyError('INITIAL_BATCH_ALREADY_STARTED')
        revalidation = None
        if revision_reader is not None:
            revalidation = revalidate_initial_capture(capture, revision_reader)
        _initial_capture_freshness(capture, revalidation)
        capture_ref = journal.evidence(capture)
        return journal.append('INITIAL_BATCH_SOURCE_BOUND', payload={
            'proposal_hash': plan.proposal_hash, 'run_id': plan.run_id,
            'batch_id': plan.batch_id, 'section_id': authority['section_id'],
            'section_manifest_ref': authority['section_manifest_ref'],
            'capture_ref': capture_ref, 'capture_sha256': capture['capture_sha256'],
            'input_sha256': capture['input_sha256'],
            'source_revision': capture['source_revision'],
            **({'revision_revalidation': revalidation} if revalidation else {}),
            'history_sequence': records[-1]['seq'] if records else 0,
            'history_tip_sha256': records[-1]['hash'] if records else None,
            'execution_approved': False})


def _require_initial_source(journal, plan, event):
    payload = event['payload']
    capture = journal.read_evidence(payload['capture_ref'])
    authority = _initial_source(journal, plan, capture, fresh=True,
                                revalidation=payload.get('revision_revalidation'))
    if (payload.get('run_id') != plan.run_id or payload.get('batch_id') != plan.batch_id or
            payload.get('section_id') != authority['section_id'] or
            payload.get('section_manifest_ref') != authority['section_manifest_ref'] or
            payload.get('capture_sha256') != capture['capture_sha256'] or
            payload.get('input_sha256') != capture['input_sha256'] or
            payload.get('source_revision') != capture['source_revision'] or
            payload.get('execution_approved') is not False):
        raise SafetyError('INITIAL_BATCH_SOURCE_BINDING_MISMATCH')
    records = journal.records()
    sequence = payload.get('history_sequence')
    if (type(sequence) is not int or sequence < 0 or sequence >= event['seq'] or
            (records[sequence - 1]['hash'] if sequence else None) != payload.get('history_tip_sha256')):
        raise SafetyError('FRESH_BATCH_HISTORY_MISMATCH')
    sources = {op.file_id for op in plan.operations}
    for later in records[sequence:]:
        p = later['payload']
        if (p.get('source_id', p.get('file_id')) in sources and later['kind'] in {
                'ATTEMPTED', 'UNCERTAIN', 'VERIFIED_APPLIED', 'RECONCILED_AT_TARGET',
                'DESIRED_ATTEMPTED', 'DESIRED_APPLIED', 'DESIRED_ROLLED_BACK', 'DESIRED_OBSERVED'}):
            raise SafetyError('HISTORY_CHANGED_SINCE_BATCH_REFRESH')


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
        initial = [e for e in records if e['kind'] == 'INITIAL_BATCH_SOURCE_BOUND'
            and e['payload'].get('proposal_hash') == plan.proposal_hash]
        if initial:
            _require_initial_source(journal, plan, initial[-1])
            return
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
