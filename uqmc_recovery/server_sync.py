"""Optional checksum-verified sync of one frozen viewer to an existing local share.

No Google API, migration, SSH, shell, mount creation or credentials are involved.
The public artifact is copied before Drive HTML delivery by the publication hook.
Receipts remain private alongside the frozen publication, never on the server.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .destination_publication import _load, _now, _save


RECEIPT_VERSION = 'uqmc.server-sync.v1'


def _read_regular(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('SERVER_SYNC_NOT_REGULAR_FILE')
        return stream.read()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _destination_hash(target):
    try:
        return _sha(_read_regular(target))
    except FileNotFoundError:
        return None


def _source(directory):
    receipt = json.loads(_read_regular(directory / 'publication.json'))
    if receipt.get('schema_version') != 'uqmc.destination.publication.v1':
        raise ValueError('SERVER_SYNC_PUBLICATION_RECEIPT_INVALID')
    expected = receipt.get('files', {}).get('viewer.html')
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('SERVER_SYNC_SOURCE_HASH_MISSING')
    data = _read_regular(directory / 'viewer.html')
    if not data.strip() or b'<html' not in data.lower():
        raise ValueError('SERVER_SYNC_SOURCE_NOT_HTML')
    if _sha(data) != expected:
        raise ValueError('SERVER_SYNC_SOURCE_HASH_MISMATCH')
    if not isinstance(receipt.get('snapshot_id'), str) or not receipt['snapshot_id']:
        raise ValueError('SERVER_SYNC_SNAPSHOT_MISSING')
    sequence = receipt.get('journal_sequence')
    created = receipt.get('created_at_utc')
    if type(sequence) is not int or sequence < 0 or not isinstance(created, str):
        raise ValueError('SERVER_SYNC_PUBLICATION_ORDER_MISSING')
    try:
        stamp = datetime.fromisoformat(created.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('SERVER_SYNC_PUBLICATION_ORDER_INVALID') from None
    return receipt['snapshot_id'], expected, sequence, stamp.isoformat()


def _transfer(source, target):
    # rsync's default temporary-file/rename behavior is retained; no --inplace.
    # subprocess.run kills and waits for this local process on timeout. Capture
    # its output privately in memory only, avoiding host/path/error disclosure.
    result = subprocess.run(['rsync', '-c', '--progress', '--', str(source), str(target)],
                            capture_output=True, timeout=60, check=False)
    return result.returncode


def _error(exc):
    code = str(exc) if isinstance(exc, ValueError) else ''
    return {'reason': code if re.fullmatch(r'[A-Z][A-Z0-9_]+', code) else 'SERVER_SYNC_IO_ERROR',
            'error_class': type(exc).__name__}


def sync_publication(directory, config=None, *, apply=False, confirm_transfer_stopped=False,
                     transfer=None):
    """Check/copy only viewer.html to the configured existing share's index.html.

    No automatic second dispatch within a call. Later calls reconcile hashes.
    An interrupted process with unchanged output needs explicit stopped-process
    confirmation; changed output is a conflict, not permission to overwrite it.
    """
    if not config or config.get('enabled') is not True:
        return {'state': 'DISABLED'}
    directory = Path(directory).resolve()
    receipt_path = directory / 'server-sync.json'
    transfer = transfer or _transfer

    def run():
        snapshot, source_hash, sequence, created = _source(directory)
        configured = config.get('destination_directory')
        if not isinstance(configured, str) or not configured or not Path(configured).is_absolute():
            raise ValueError('SERVER_SYNC_DESTINATION_DIRECTORY_REQUIRED')
        destination = Path(configured)
        if destination.is_symlink():
            raise ValueError('SERVER_SYNC_DESTINATION_SYMLINK')
        if not destination.is_dir():
            raise ValueError('SERVER_SYNC_DESTINATION_UNAVAILABLE')
        destination = destination.resolve(strict=True)
        destination_stat = destination.stat()
        destination_identity = [destination_stat.st_dev, destination_stat.st_ino]
        target = destination / 'index.html'
        binding = {'schema_version': RECEIPT_VERSION, 'snapshot_id': snapshot,
                   'source_sha256': source_hash, 'journal_sequence': sequence,
                   'publication_created_at_utc': created, 'destination_directory': str(destination),
                   'target_name': 'index.html'}
        prior = _load(receipt_path) if receipt_path.exists() else None
        if prior and any(prior.get(k) != v for k, v in binding.items()):
            raise ValueError('SERVER_SYNC_RECEIPT_BINDING_MISMATCH')
        latest_path = directory.parent / '.server-sync-latest.json'
        latest = json.loads(_read_regular(latest_path)) if latest_path.exists() else {
            'schema_version': 'uqmc.server-sync.latest.v1', 'targets': {}}
        if latest.get('schema_version') != 'uqmc.server-sync.latest.v1' or not isinstance(latest.get('targets'), dict):
            raise ValueError('SERVER_SYNC_LATEST_RECEIPT_INVALID')
        target_key = _sha(str(target).encode())
        previous_delivery = latest['targets'].get(target_key)
        order = (sequence, datetime.fromisoformat(created))
        previous_order = None
        if previous_delivery:
            previous_order = (previous_delivery['journal_sequence'],
                              datetime.fromisoformat(previous_delivery['publication_created_at_utc']))
            if order <= previous_order and previous_delivery['source_sha256'] != source_hash:
                raise ValueError('SERVER_SYNC_STALE_PUBLICATION')
        observed_hash = _destination_hash(target)
        if previous_delivery and previous_delivery.get('state') in {'ATTEMPTED', 'UNCERTAIN'}:
            same_delivery = (previous_delivery['snapshot_id'] == snapshot and
                             previous_delivery['source_sha256'] == source_hash)
            if not same_delivery:
                if observed_hash != previous_delivery['source_sha256']:
                    raise ValueError('SERVER_SYNC_PRIOR_DELIVERY_UNRESOLVED')
                # A newer delivery may proceed only after the previous intended
                # bytes are observed; attribution remains in its own receipt.
                latest['targets'][target_key] = {**previous_delivery, 'state': 'VERIFIED',
                                                 'verified_at_utc': _now(), 'reconciled': True}
                if apply:
                    _save(latest_path, latest)
            elif prior is None or prior['state'] not in {'ATTEMPTED', 'UNCERTAIN', 'VERIFIED'}:
                # Covers interruption after the target-wide intent was fsynced
                # but before the per-publication ATTEMPTED receipt was committed.
                prior = {**(prior or {}), 'state': 'ATTEMPTED',
                         'before_sha256': previous_delivery.get('before_sha256')}

        def record(state, **fields):
            value = {**binding,
                     **({'before_sha256': prior['before_sha256']} if prior and 'before_sha256' in prior else {}),
                     'state': state, 'updated_at_utc': _now(),
                     'attempt': prior.get('attempt', 0) if prior else 0, **fields}
            if apply:
                if (state in {'ATTEMPTED', 'VERIFIED', 'UNCERTAIN', 'FAILED_NOT_APPLIED'}
                        and (previous_order is None or order >= previous_order)):
                    # The target-wide write-ahead intent protects the success-to-
                    # receipt crash window too: an older unsent artifact cannot
                    # adopt this newer delivery as an overwriteable preimage.
                    latest['targets'][target_key] = {
                        'snapshot_id': snapshot, 'source_sha256': source_hash, 'state': state,
                        'journal_sequence': sequence, 'publication_created_at_utc': created,
                        'before_sha256': value.get('before_sha256'), 'updated_at_utc': _now(),
                        **({'verified_at_utc': _now()} if state == 'VERIFIED' else {})}
                    _save(latest_path, latest)
                _save(receipt_path, value)
            return {k: v for k, v in value.items() if k not in {'destination_directory'}} | {
                'receipt_path': str(receipt_path)}

        if observed_hash == source_hash:
            return record('VERIFIED', observed_sha256=observed_hash,
                          reconciled=True, verified_at_utc=_now())
        if prior:
            if prior['state'] == 'VERIFIED' or observed_hash != prior.get('before_sha256'):
                return record('CONFLICT', reason='SERVER_SYNC_DESTINATION_CHANGED',
                              before_sha256=prior.get('before_sha256'), observed_sha256=observed_hash)
            if prior['state'] in {'ATTEMPTED', 'UNCERTAIN'} and not confirm_transfer_stopped:
                return record('UNCERTAIN', reason='SERVER_SYNC_PRIOR_TRANSFER_UNRESOLVED',
                              before_sha256=prior.get('before_sha256'), observed_sha256=observed_hash)
            if prior['state'] == 'CONFLICT':
                return record('CONFLICT', reason='SERVER_SYNC_DESTINATION_CHANGED',
                              before_sha256=prior.get('before_sha256'), observed_sha256=observed_hash)
        if not apply:
            return {'state': 'READY', 'snapshot_id': snapshot, 'source_sha256': source_hash,
                    'destination_present': observed_hash is not None, 'upload_performed': False}
        attempt = (prior.get('attempt', 0) if prior else 0) + 1
        record('PREPARED', attempt=attempt, before_sha256=observed_hash)
        # Reject local artifact or target changes after validation, before copy.
        if _source(directory) != (snapshot, source_hash, sequence, created):
            raise ValueError('SERVER_SYNC_SOURCE_CHANGED')
        if (not destination.is_dir() or destination.is_symlink() or
                destination.resolve(strict=True) != destination or
                [destination.stat().st_dev, destination.stat().st_ino] != destination_identity or
                _destination_hash(target) != observed_hash):
            return record('CONFLICT', reason='SERVER_SYNC_DESTINATION_CHANGED',
                          attempt=attempt, before_sha256=observed_hash)
        record('ATTEMPTED', attempt=attempt, before_sha256=observed_hash,
               prior_transfer_confirmed_stopped=bool(confirm_transfer_stopped))
        failure = None
        returncode = None
        terminal = False
        try:
            returncode = transfer(directory / 'viewer.html', target)
            terminal = True
            if returncode:
                failure = {'reason': 'SERVER_SYNC_TRANSFER_FAILED', 'returncode': returncode}
        except subprocess.TimeoutExpired:
            # The built-in transfer waits for the killed local rsync process.
            terminal = True
            failure = {'reason': 'SERVER_SYNC_TRANSFER_TIMEOUT', 'error_class': 'TimeoutExpired'}
        except OSError as exc:
            # Launch failure is terminal; rsync has not been started by run().
            terminal = True
            failure = _error(exc)
        except Exception as exc:
            failure = _error(exc)
        try:
            after_hash = _destination_hash(target)
        except Exception as exc:
            return record('UNCERTAIN', attempt=attempt, before_sha256=observed_hash,
                          reason='SERVER_SYNC_READBACK_UNAVAILABLE', failure=failure, **{'read_error_class': type(exc).__name__})
        if after_hash == source_hash:
            return record('VERIFIED', attempt=attempt, before_sha256=observed_hash,
                          observed_sha256=after_hash, verified_at_utc=_now(), failure=failure,
                          reconciled=bool(failure))
        if terminal and after_hash == observed_hash:
            return record('FAILED_NOT_APPLIED', attempt=attempt, before_sha256=observed_hash,
                          observed_sha256=after_hash, failure=failure,
                          reason='SERVER_SYNC_TRANSFER_NOT_APPLIED')
        return record('UNCERTAIN', attempt=attempt, before_sha256=observed_hash,
                      observed_sha256=after_hash, failure=failure,
                      reason='SERVER_SYNC_OUTCOME_UNCERTAIN')

    try:
        if not apply:
            return run()
        # Serializes this project's automatic and server-only publication calls.
        # Never creates anything in an absent destination/mount.
        with (directory.parent / '.server-sync.lock').open('a+b') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return run()
    except Exception as exc:
        return {'state': 'BLOCKED', **_error(exc), 'receipt_path': str(receipt_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication', required=True, help='Saved publication directory containing viewer.html and publication.json')
    parser.add_argument('--config', default='private/destination-config.json')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='Validate and compare checksums without uploading')
    mode.add_argument('--apply', action='store_true', help='Sync only this verified local artifact; no Google calls or migration')
    parser.add_argument('--confirm-transfer-stopped', action='store_true',
                        help='After confirming interrupted rsync has stopped, allow an unchanged-target retry')
    args = parser.parse_args(argv)
    if args.confirm_transfer_stopped and not args.apply:
        parser.error('--confirm-transfer-stopped requires --apply')
    try:
        config = _load(args.config).get('server_sync')
        result = sync_publication(args.publication, config, apply=args.apply,
                                  confirm_transfer_stopped=args.confirm_transfer_stopped)
    except Exception as exc:
        result = {'state': 'BLOCKED', **_error(exc)}
    print(json.dumps(result))
    return 0 if result['state'] in {'READY', 'VERIFIED', 'DISABLED'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
