"""Incremental local checkpoints; evidence bytes are shared only between backups.

The source evidence is never hard-linked. A private index avoids rereading blobs
whose device/inode/size/mtime/ctime match their last verification. This is a cache
for cooperating local processes, not protection against the account owner or
silent disk corruption. Reading evidence during recovery still verifies SHA-256.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any
import uuid

from . import journal as j


def _stat(path: Path) -> list[int]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise j.JournalCorrupt(f"Expected a regular evidence file: {path}")
    return list(j._fingerprint(info))


def _sealed(value: dict) -> bytes:
    return j.canonical({**value, "index_sha256": j.digest(j.canonical(value))})


def _index(path: Path) -> dict:
    try:
        value = json.loads(j._read(path))
        checksum = value.pop("index_sha256")
        if value.get("schema_version") == 1 and j.digest(j.canonical(value)) == checksum:
            return value["entries"]
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        pass
    # The index is dispensable. Its absence/corruption requires revalidation.
    return {}


def _save_index(path: Path, entries: dict) -> None:
    data = _sealed({"schema_version": 1, "entries": entries})
    pending = path.parent / (".index-" + str(uuid.uuid4()))
    j._new_file(pending, data)
    os.replace(pending, path)
    j._fsync_dir(path.parent)
    if j._read(path) != data:
        raise j.JournalCorrupt("Evidence index readback failed")


def _seed(parent: Path, source_root: Path) -> tuple[Path | None, dict]:
    """Use one recent complete legacy checkpoint; never modify its bytes."""
    candidates = []
    for path in parent.iterdir():
        if path.name.startswith('.') or path.is_symlink() or not path.is_dir():
            continue
        manifest = path / 'snapshot-manifest.json'
        if manifest.is_file() and not (path / '.snapshot-incomplete').exists():
            candidates.append(manifest)
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:10]:
        try:
            value = json.loads(j._read(path))
            checksum = value.pop('manifest_sha256')
            if (value.get('source_path') == str(source_root)
                    and j.digest(j.canonical(value)) == checksum
                    and isinstance(value.get('evidence_sha256'), dict)):
                seed = path.parent / 'evidence'
                if seed.is_dir() and not seed.is_symlink():
                    return seed, value['evidence_sha256']
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None, {}


@contextmanager
def _pool(parent: Path):
    root = parent / '.snapshot-evidence'
    j._private_directory(root)
    blobs = root / 'blobs'
    j._private_directory(blobs)
    fd = j._open(root / '.lock', os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root, blobs
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _publish_blob(blobs: Path, name: str, raw: bytes) -> None:
    # An interrupted staging file is never mistaken for a complete blob.
    pending = blobs.parent / ('.pending-' + str(uuid.uuid4()))
    j._new_file(pending, raw)
    os.link(pending, blobs / name)
    j._fsync_dir(blobs)
    pending.unlink()
    j._fsync_dir(blobs.parent)


def _references_present(value: Any, names: set[str]) -> None:
    if isinstance(value, str) and value.startswith('sha256:') and j._DIGEST.fullmatch(value[7:]):
        if value[7:] + '.json' not in names:
            raise j.JournalCorrupt('Missing or invalid evidence')
    elif isinstance(value, dict):
        for child in value.values():
            _references_present(child, names)
    elif isinstance(value, list):
        for child in value:
            _references_present(child, names)


def incremental_backup(journal: j.Journal, destination: str | Path) -> dict:
    with journal.locked():
        journal._complete()
        journal._check_cache_stat()
        destination = Path(destination).absolute()
        if destination == journal.root:
            raise ValueError('Snapshot destination must be a NEW directory')
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        source = j._read(journal.path)
        records = j._validate(source)
        paths = sorted(journal.evidence_dir.iterdir())
        names = set()
        for path in paths:
            if path.suffix != '.json' or not j._DIGEST.fullmatch(path.stem):
                raise j.JournalCorrupt('Unexpected file in content-addressed evidence store')
            names.add(path.name)
        for record in records:
            _references_present(record['payload'], names)
        with _pool(destination.parent) as (root, blobs):
            entries = _index(root / 'index.json')
            seed_root, seed_hashes = _seed(destination.parent, journal.root)
            stats = {'copied_blob_count': 0, 'copied_blob_bytes': 0,
                     'seeded_blob_count': 0, 'reused_blob_count': 0,
                     'source_verified_blob_count': 0}
            cached_sources = {}
            prepared = {}
            for path in paths:
                fp = _stat(path)
                blob = blobs / path.name
                entry = entries.get(path.name, {})
                sources = entry.get('sources', {})
                source_cached = sources.get(str(journal.root)) == fp
                raw = None
                if not source_cached:
                    raw = j._read(path)
                    j._decode_evidence(raw, path.stem)
                    stats['source_verified_blob_count'] += 1
                else:
                    cached_sources[path] = fp
                if blob.exists() or blob.is_symlink():
                    if entry.get('pool_fingerprint') != _stat(blob):
                        if j.digest(j._read(blob)) != path.stem:
                            raise j.JournalCorrupt('Backup evidence content hash mismatch')
                    stats['reused_blob_count'] += 1
                else:
                    seed = seed_root / path.name if seed_root else None
                    if seed and seed_hashes.get(path.name) == path.stem and seed.is_file():
                        _stat(seed)
                        if j.digest(j._read(seed)) != path.stem:
                            raise j.JournalCorrupt('Legacy backup evidence content hash mismatch')
                        os.link(seed, blob)
                        j._fsync_dir(blobs)
                        stats['seeded_blob_count'] += 1
                    else:
                        if raw is None:
                            raw = j._read(path)
                            j._decode_evidence(raw, path.stem)
                            stats['source_verified_blob_count'] += 1
                        _publish_blob(blobs, path.name, raw)
                        stats['copied_blob_count'] += 1
                        stats['copied_blob_bytes'] += len(raw)
                sources = {**sources, str(journal.root): fp}
                prepared[path.name] = {'sources': sources, 'pool_fingerprint': _stat(blob)}
            # A stat-only cache hit must not conceal a change during this capture.
            for path, fp in cached_sources.items():
                if _stat(path) != fp:
                    raise j.JournalCorrupt('Source evidence changed during snapshot')
            j._new_snapshot_directory(destination)
            evidence = destination / 'evidence'
            evidence.mkdir(mode=0o700)
            j._fsync_dir(destination)
            j._new_file(destination / 'events.jsonl', source)
            j._new_file(destination / '.lock', b'')
            for name, entry in prepared.items():
                blob = blobs / name
                # A hard link shares verified backup bytes, never mutable source bytes.
                before = _stat(blob)
                if before != entry['pool_fingerprint']:
                    raise j.JournalCorrupt('Backup evidence changed during snapshot')
                os.link(blob, evidence / name)
                after = _stat(blob)
                if after[:4] != before[:4] or _stat(evidence / name) != after:
                    raise j.JournalCorrupt('Snapshot evidence link verification failed')
                entry['pool_fingerprint'] = after  # link creation changes ctime
            j._fsync_dir(evidence)
            manifest = {
                'schema_version': 1, 'kind': 'backup', 'created_at': j._now(),
                'source_path': str(journal.root), 'source_journal_sha256': j.digest(source),
                'copied_journal_sha256': j.digest(source), 'event_count': len(records),
                'head_hash': records[-1]['hash'] if records else j.GENESIS,
                'evidence_sha256': {name: name[:-5] for name in sorted(names)},
                'discarded_terminal_byte_count': 0, 'discarded_terminal_sha256': None,
                'reconciliation_required': False,
                'pending_evidence_excluded': len(list(journal.pending_dir.iterdir())),
                'evidence_storage': 'shared_verified_backup_blobs_v1',
                'evidence_copy_stats': stats,
            }
            manifest['manifest_sha256'] = j.digest(j.canonical(manifest))
            j._new_file(destination / 'snapshot-manifest.json', j.canonical(manifest))
            if j._validate(j._read(destination / 'events.jsonl')) != records:
                raise j.JournalCorrupt('Snapshot journal verification failed')
            entries.update(prepared)
            _save_index(root / 'index.json', entries)
            (destination / '.snapshot-incomplete').unlink()
            j._fsync_dir(destination)
            j._fsync_dir(destination.parent)
            return manifest
