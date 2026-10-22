"""Recoverable status delivery, independent of destination execution.

The caller supplies a frozen journal view, the fresh proposal capture and its run
registry. Only explicit apply publishes. A retry operates on saved artifacts and
never imports, enables or executes a migration. Private artifacts must be outside
the served viewer directory. The existing Drive transport supplies authentication
and the 15-second socket timeout; this module never changes credentials/scopes.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile

from .publication_projection import build_publication
from .projection import validate_snapshot
from .sheet_publication import publish as publish_sheet
from . import viewer_publication as viewer


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _error(exc):
    # Our stable uppercase reason codes are safe and actionable. Never expose
    # arbitrary Google exception text, URLs, headers or token-bearing messages.
    text = str(exc) if isinstance(exc, ValueError) else ''
    code = text.split(':', 1)[0]
    return {'error_class': type(exc).__name__, **({'reason': code}
            if re.fullmatch(r'[A-Z][A-Z0-9_]+', code) else {})}


def _save(path, value):
    """Atomic owner-private file plus parent-directory fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = value if isinstance(value, bytes) else _bytes(value)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _load(path):
    return json.loads(Path(path).read_text())


def section_specs(registry):
    """Registry publication_spec is produced by the destination planner/runner.

    Historical specs may be seeded once in the project configuration. No section
    mapping is solicited or inferred during publishing. The projection checks each
    spec against exact journal evidence; a registry COMPLETED flag proves nothing.
    """
    entries = registry.get('sections', []) if isinstance(registry, dict) else registry
    result = []
    seen = set()
    required = {'section_id', 'title', 'proposal_path', 'source_ids',
                'destination_id', 'destination_mode'}
    for entry in entries:
        spec = copy.deepcopy(entry.get('publication_spec', entry))
        if not required <= spec.keys() or not (spec.get('plan_sha256') or spec.get('plan_sha256s')):
            raise ValueError('REGISTRY_PUBLICATION_SPEC_INCOMPLETE')
        if spec['section_id'] in seen:
            raise ValueError('DUPLICATE_REGISTRY_SECTION')
        seen.add(spec['section_id'])
        result.append(spec)
    return result


def _metadata_identity(raw, file_id):
    if raw.get('id') != file_id or raw.get('trashed') is not False:
        raise ValueError('VIEWER_IDENTITY_OR_TRASH_CONFLICT')
    if raw.get('mimeType') != 'text/html' or not isinstance(raw.get('name'), str):
        raise ValueError('VIEWER_NOT_EXISTING_HTML')
    if not isinstance(raw.get('parents'), list):
        raise ValueError('VIEWER_PARENTS_UNAVAILABLE')
    # A content-only files.update cannot change sharing. No permission listing is
    # needed or made. Identity/name/parents/MIME are checked again after delivery.
    return {k: copy.deepcopy(raw[k]) for k in ('id', 'name', 'parents', 'mimeType', 'trashed')}


def _read_viewer(drive, file_id):
    before = drive.metadata(file_id)
    content = drive.download(file_id)
    after = drive.metadata(file_id)
    identity = _metadata_identity(before, file_id)
    if identity != _metadata_identity(after, file_id):
        raise ValueError('VIEWER_CHANGED_DURING_READ')
    if before.get('version') != after.get('version'):
        raise ValueError('VIEWER_VERSION_CHANGED_DURING_READ')
    if not isinstance(content, bytes):
        raise ValueError('VIEWER_DOWNLOAD_NOT_BYTES')
    return identity, content


def display_section_specs(registry, data, allowed_source_ids):
    """Count all current viewer members without expanding execution authority.

    The registry remains unchanged. Extra public proposal identities have no
    matching sealed execution plan, so build_publication leaves them unverified.
    A missing registered identity or ambiguous/unknown viewer identity is a
    conflict, not a reason to reduce the expected completion count.
    """
    allowed = set(allowed_source_ids)
    paths = {}
    for node in data['nodes']:
        paths[node['i']] = (paths[node['p']] if node['p'] >= 0 else ()) + (node['n'].strip(),)
    specs = section_specs(registry)
    for spec in specs:
        root = viewer.resolve_folder_path(data, spec['proposal_path'])
        path = paths[root['i']]
        ids = [n.get('id') for n in data['nodes'] if n['s'] == 'destination' and not n['f']
               and paths[n['i']][:len(path)] == path]
        if (any(not x for x in ids) or len(ids) != len(set(ids))
                or not set(ids) <= allowed):
            raise ValueError('SECTION_DISPLAY_IDENTITIES_INVALID:' + spec['section_id'])
        registered = spec['source_ids']
        if len(registered) != len(set(registered)) or not set(registered) <= set(ids):
            raise ValueError('SECTION_REGISTERED_MEMBER_MISSING:' + spec['section_id'])
        spec['source_ids'] = sorted(ids)
    # A later child-section run can finish previously untracked parent members.
    # Aggregate only sealed plans from registered descendant destinations; the
    # projection still requires exact verified state matching those plan targets.
    for parent in specs:
        parent_path = paths[viewer.resolve_folder_path(data, parent['proposal_path'])['i']]
        hashes = list(parent.get('plan_sha256s', [parent.get('plan_sha256')]))
        for child in specs:
            child_path = paths[viewer.resolve_folder_path(data, child['proposal_path'])['i']]
            if len(child_path) > len(parent_path) and child_path[:len(parent_path)] == parent_path:
                hashes.extend(child.get('plan_sha256s', [child.get('plan_sha256')]))
        parent['plan_sha256s'] = list(dict.fromkeys(hashes))
    return specs


def freeze_publication(config, registry, *, journal, fresh_workbook, drive):
    """Read remote HTML; freeze private artifacts without any remote writes."""
    basis = config['identity_basis']
    if isinstance(basis, (str, Path)):
        basis = _load(basis)
    metadata, source = _read_viewer(drive, config['viewer_file_id'])
    data = viewer.unpack(source.decode('utf-8'))
    identities = {n['id'] for n in data['nodes'] if n.get('id')}
    workbook = copy.deepcopy(fresh_workbook)
    workbook.setdefault('exported_at_utc', workbook.get('captured_at_utc', _now()))
    report = viewer.refresh_proposal(data, workbook)
    specs = display_section_specs(registry, data, basis['source_ids'])
    bundle = build_publication(journal, allowed_source_ids=basis['source_ids'],
        identity_basis=basis, sections=specs)
    snapshot = bundle['html']['snapshot_id']
    # Proposal edits may change HTML without changing journal snapshot. Include
    # proposal content in the delivery key; snapshot IDs still match sheet/HTML.
    proposal_hash = _sha(_bytes(fresh_workbook))
    delivery_key = snapshot + '-' + proposal_hash[:16]
    directory = Path(config['publication_directory']) / delivery_key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / '.lock').open('a+b') as lock:
        os.chmod(directory / '.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = directory / 'publication.json'
        if manifest.exists():
            existing = _load(manifest)
            if existing['workbook_id'] != config['workbook_id'] or existing['viewer_file_id'] != config['viewer_file_id']:
                raise ValueError('PUBLICATION_TARGET_CHANGED')
            return directory
        validate_snapshot(bundle['html'])
        viewer.integrate(data, bundle)
        if {n['id'] for n in data['nodes'] if n.get('id')} != identities:
            raise ValueError('PUBLIC_SOURCE_IDENTITIES_CHANGED')
        target = viewer.render(source.decode('utf-8'), data).encode('utf-8')
        files = {'bundle.json': _bytes(bundle), 'workbook.json': _bytes(workbook),
                 'viewer-before.html': source, 'viewer.html': target}
        for name, value in files.items():
            _save(directory / name, value)
        _save(manifest, dict(schema_version='uqmc.destination.publication.v1',
            snapshot_id=snapshot, journal_sequence=bundle['html']['journal_sequence'],
            created_at_utc=_now(), workbook_id=config['workbook_id'],
            viewer_file_id=config['viewer_file_id'], viewer_identity=metadata,
            proposal_sha256=proposal_hash, files={k: _sha(v) for k, v in files.items()},
            sheet={'state': 'NOT_ATTEMPTED'}, html={'state': 'NOT_ATTEMPTED'},
            proposal_report=report))
    return directory


def _artifacts(directory, receipt):
    result = {}
    for name in ('bundle.json', 'workbook.json', 'viewer-before.html', 'viewer.html'):
        value = (directory / name).read_bytes()
        if _sha(value) != receipt['files'][name]:
            raise ValueError('PUBLICATION_ARTIFACT_HASH_MISMATCH')
        result[name] = value
    return result


def _sheet_readback_saved(capture, stage):
    """Recover a crash after remote success from the existing publisher preimage."""
    from .sheet_publication import pad, read_existing, seal, BASE_WIDTHS
    prewrite = stage / 'prewrite.json'
    if not prewrite.exists():
        return False
    plan = _load(prewrite)['plan']
    api = capture.service.spreadsheets()
    for start in range(0, len(plan['changes']), 100):
        chunk = plan['changes'][start:start + 100]
        response = api.values().batchGet(spreadsheetId=capture.workbook_id,
            ranges=[c['range'] for c in chunk], valueRenderOption='FORMULA').execute(num_retries=0)
        if len(response.get('valueRanges', [])) != len(chunk):
            return False
        for change, observed in zip(chunk, response['valueRanges']):
            expected = [[v if v is not None else '' for v in row] for row in change['values']]
            actual = observed.get('values', [])
            actual = [pad(actual[n] if n < len(actual) else [], len(expected[0])) for n in range(len(expected))]
            if actual != expected:
                return False
    current = read_existing(capture)
    return seal({n: [pad(r, w) for r in current['matrices'][n]] for n, w in BASE_WIDTHS.items()}) == plan['preserved_digest']


def resume_publication(directory, *, sheet_capture, drive, apply=False, server_sync=None):
    """Retry only saved publication. Both stages reconcile before any dispatch.

    Sheet conflicts/errors do not prevent an independent HTML attempt. Receipts
    retain stage state and sanitized exception class; raw credential-bearing
    exception text is never exported. There is no exactly-once claim: Drive and
    Sheets offer no atomic compare-and-swap for these publication operations.
    """
    directory = Path(directory)
    with (directory / '.lock').open('a+b') as lock:
        os.chmod(directory / '.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'publication.json'
        receipt = _load(path)
        files = _artifacts(directory, receipt)
        if sheet_capture.workbook_id != receipt['workbook_id']:
            raise ValueError('PUBLICATION_WORKBOOK_MISMATCH')
        bundle = json.loads(files['bundle.json'])
        if not apply:
            return {'result': 'PREVIEW', 'snapshot_id': receipt['snapshot_id'],
                    'directory': str(directory), 'html_path': str(directory / 'viewer.html'),
                    'journal_sequence': receipt['journal_sequence']}
        server_result = {'state': 'DISABLED'}
        for kind in ('sheet', 'server', 'html'):
            if kind == 'server':
                # The same frozen local bytes go to the mounted share first.
                # Sheets remain independent; enabled server verification gates
                # a new Drive upload of these same bytes.
                from .server_sync import sync_publication
                server_result = sync_publication(directory, server_sync, apply=True)
                receipt['server_sync'] = server_result
                _save(path, receipt)
                continue
            if (kind == 'html' and server_sync and server_sync.get('enabled') is True
                    and server_result['state'] != 'VERIFIED'):
                # Preserve prior verified/uncertain Drive outcomes honestly.
                # A later publication-only retry can resume after server recovery.
                if receipt['html']['state'] not in {'VERIFIED', 'ATTEMPTED', 'UNCERTAIN'}:
                    receipt['html'] = {'state': 'WAITING_FOR_SERVER', 'reason': 'SERVER_NOT_VERIFIED'}
                    _save(path, receipt)
                continue
            try:
                if kind == 'sheet':
                    stage = directory / 'sheet'
                    state = receipt[kind]['state']
                    if state != 'NOT_ATTEMPTED' and _sheet_readback_saved(sheet_capture, stage):
                        receipt[kind] = {'state': 'VERIFIED', 'verified_at_utc': _now(),
                                         'reconciled_saved_delivery': True}
                    else:
                        # Build against current cells to preserve intervening human
                        # proposal edits. Refuse occupied publication columns as usual.
                        receipt[kind] = {'state': 'ATTEMPTED', 'attempted_at_utc': _now()}
                        _save(path, receipt)
                        result = publish_sheet(sheet_capture, bundle, stage, apply=True)
                        receipt[kind] = {'state': 'VERIFIED' if result['result'] == 'PUBLICATION_VERIFIED' else 'CONFLICT',
                                         'result': result, 'verified_at_utc': _now()}
                else:
                    fid = receipt['viewer_file_id']
                    identity, observed = _read_viewer(drive, fid)
                    if identity != receipt['viewer_identity']:
                        raise ValueError('VIEWER_IDENTITY_CHANGED')
                    target = files['viewer.html']
                    if observed == target:
                        receipt[kind] = {'state': 'VERIFIED', 'verified_at_utc': _now(),
                                         'sha256': _sha(target), 'reconciled_saved_delivery': True}
                    elif observed != files['viewer-before.html']:
                        receipt[kind] = {'state': 'CONFLICT', 'reason': 'VIEWER_CONTENT_CHANGED',
                                         'observed_sha256': _sha(observed)}
                    else:
                        receipt[kind] = {'state': 'ATTEMPTED', 'attempted_at_utc': _now(),
                                         'sha256': _sha(target)}
                        _save(path, receipt)  # Write-ahead fence before the only upload.
                        write_error = None
                        try:
                            drive.replace_content(fid, target)
                        except Exception as exc:
                            write_error = type(exc).__name__
                        identity, observed = _read_viewer(drive, fid)
                        if identity != receipt['viewer_identity']:
                            receipt[kind] = {'state': 'CONFLICT', 'reason': 'VIEWER_IDENTITY_CHANGED'}
                        elif observed == target:
                            receipt[kind] = {'state': 'VERIFIED', 'verified_at_utc': _now(),
                                'sha256': _sha(target), 'write_error_class': write_error}
                        else:
                            receipt[kind] = {'state': 'UNCERTAIN', 'reason': 'UPLOAD_NOT_VERIFIED',
                                'observed_sha256': _sha(observed), 'write_error_class': write_error}
            except Exception as exc:
                receipt[kind] = {'state': 'UNCERTAIN' if receipt[kind]['state'] == 'ATTEMPTED' else 'BLOCKED',
                                 **_error(exc)}
            _save(path, receipt)
        return {'result': 'PUBLICATION_VERIFIED' if (all(receipt[x]['state'] == 'VERIFIED' for x in ('sheet', 'html'))
                and (not server_sync or server_sync.get('enabled') is not True or server_result['state'] == 'VERIFIED')) else 'PUBLICATION_INCOMPLETE',
            'snapshot_id': receipt['snapshot_id'], 'journal_sequence': receipt['journal_sequence'],
            'sheet': receipt['sheet'], 'html': receipt['html'], 'server_sync': server_result, 'directory': str(directory),
            'html_path': str(directory / 'viewer.html'),
            'viewer_url': 'https://drive.google.com/file/d/' + receipt['viewer_file_id'] + '/view'}


def publish_registered_sections(config, registry, *, journal, fresh_workbook,
                                sheet_capture, drive, apply=False):
    directory = freeze_publication(config, registry, journal=journal,
        fresh_workbook=fresh_workbook, drive=drive)
    return resume_publication(directory, sheet_capture=sheet_capture, drive=drive, apply=apply,
                              server_sync=config.get('server_sync'))


class GooglePublicationDrive:
    """Narrow content-only adapter over the existing authorized Drive transport."""
    def __init__(self, transport, viewer_file_id, *, apply=False):
        transport._guard(viewer_file_id)
        self.transport = transport
        self.file_id = viewer_file_id
        self.apply = apply

    def _guard(self, file_id):
        if file_id != self.file_id:
            raise ValueError('OUTSIDE_VIEWER_PUBLICATION_SCOPE')
        self.transport._guard(file_id)
        self.transport._ready(item_id=file_id)

    def metadata(self, file_id):
        self._guard(file_id)
        return self.transport.service.files().get(fileId=file_id,
            fields='id,name,parents,mimeType,trashed,version',
            supportsAllDrives=True).execute(num_retries=0)

    def download(self, file_id):
        self._guard(file_id)
        return self.transport.service.files().get_media(fileId=file_id,
            supportsAllDrives=True).execute(num_retries=0)

    def replace_content(self, file_id, content):
        self._guard(file_id)
        if not self.apply:
            raise ValueError('PUBLICATION_APPLY_REQUIRED')
        from googleapiclient.http import MediaIoBaseUpload
        return self.transport.service.files().update(fileId=file_id,
            media_body=MediaIoBaseUpload(io.BytesIO(content), mimetype='text/html', resumable=False),
            fields='id,name,parents,mimeType,version', supportsAllDrives=True).execute(num_retries=0)
