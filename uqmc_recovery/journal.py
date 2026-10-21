"""Private, append-only local write-ahead journal.

The hash chain detects accidental changes when a trusted head is retained; it is
not a signature, WORM device, or protection against the Mac account owner.
Successful writes are fsynced and read back. Hardware/filesystem guarantees and
off-device backup policy remain deployment concerns.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator
import uuid


GENESIS = "0" * 64
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FIELDS = {"schema_version", "seq", "event_id", "at", "kind", "operation_id",
           "attempt", "payload", "prev_hash", "hash"}


class JournalCorrupt(RuntimeError):
    """Evidence or journal integrity is uncertain: do not dispatch mutations."""


class JournalBusy(RuntimeError):
    """Another thread/process holds the store; no operation was acquired."""


def canonical(data: Any) -> bytes:
    """The sole serialization used for hashes and durable JSON records."""
    return json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_directory(path: Path, *, create: bool = True) -> None:
    if path.is_symlink():
        raise JournalCorrupt(f"Refusing symlink directory: {path}")
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise JournalCorrupt(f"Expected a directory: {path}")
    os.chmod(path, 0o700)


def _open(path: Path, flags: int) -> int:
    try:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        if path.is_symlink():
            raise JournalCorrupt(f"Refusing symlink file: {path}") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise JournalCorrupt(f"Expected a regular file: {path}")
        # Repeating fchmod can change ctime even when mode is unchanged.
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(path: Path) -> bytes:
    fd = _open(path, os.O_RDONLY)
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("Write made no progress")
        view = view[count:]


def _new_file(path: Path, data: bytes) -> None:
    """Never replace an existing file, including a failed/partial earlier write."""
    fd = _open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)
    if _read(path) != data:
        raise JournalCorrupt(f"Durable file readback failed: {path}")


def _validate_record(line: bytes, index: int, previous: str) -> dict[str, Any]:
    """Validate one canonical entry against the already verified chain tip."""
    try:
        record = json.loads(line)
        if not isinstance(record, dict) or set(record) != _FIELDS:
            raise ValueError("Unexpected fields")
        if canonical(record) != line:
            raise ValueError("Non-canonical record")
        if record["schema_version"] != 1 or type(record["seq"]) is not int or record["seq"] != index:
            raise ValueError("Version or sequence mismatch")
        if record["prev_hash"] != previous:
            raise ValueError("Broken chain")
        if not isinstance(record["kind"], str) or not record["kind"]:
            raise ValueError("Missing event kind")
        if record["operation_id"] is not None and not isinstance(record["operation_id"], str):
            raise ValueError("Invalid operation ID")
        if type(record["attempt"]) is not int or record["attempt"] < 0:
            raise ValueError("Invalid attempt")
        if not isinstance(record["payload"], dict):
            raise ValueError("Invalid payload")
        uuid.UUID(record["event_id"])
        if not record["at"].endswith("Z"):
            raise ValueError("Timestamp must be UTC")
        datetime.fromisoformat(record["at"].replace("Z", "+00:00"))
        body = {key: value for key, value in record.items() if key != "hash"}
        if record["hash"] != digest(canonical(body)):
            raise ValueError("Record hash mismatch")
        return record
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError, KeyError) as exc:
        raise JournalCorrupt(f"Invalid record {index}: {exc}") from exc


def _validate(raw: bytes) -> list[dict[str, Any]]:
    if raw and not raw.endswith(b"\n"):
        raise JournalCorrupt("Incomplete terminal record; recover to a NEW store")
    result: list[dict[str, Any]] = []
    previous = GENESIS
    for index, line in enumerate(raw.splitlines(), 1):
        record = _validate_record(line, index, previous)
        result.append(record)
        previous = record["hash"]
    return result


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class Journal:
    """One private store. All accesses share its advisory process lock.

    Hold ``locked()`` around reconciliation and remote dispatch, as well as the
    journal writes, to prevent two cooperating executors dispatching together.
    A process fork must open a new Journal instance. Locks do not restrain an
    unrelated tool or a human editing Drive directly.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()
        _private_directory(self.root)
        self.evidence_dir = self.root / "evidence"
        _private_directory(self.evidence_dir)
        # An interrupted evidence write cannot poison its content-addressed
        # final name. Incomplete staging files stay private for diagnosis.
        self.pending_dir = self.root / "pending-evidence"
        _private_directory(self.pending_dir)
        self.path = self.root / "events.jsonl"
        self.lock_path = self.root / ".lock"
        self._gate = threading.RLock()
        self._depth = 0
        self._shared = False
        self._cached_records: list[dict[str, Any]] | None = None
        self._cached_stat: tuple[int, ...] | None = None
        self._cache_poisoned = False
        self._lock_epoch = 0
        self._fd: int | None = None
        self._creator_pid = os.getpid()
        for path in (self.path, self.lock_path):
            try:
                _new_file(path, b"")
            except FileExistsError:
                fd = _open(path, os.O_RDONLY)
                os.close(fd)
        _fsync_dir(self.root)
        _fsync_dir(self.root.parent)

    @contextmanager
    def locked(self) -> Iterator[None]:
        if self._creator_pid != os.getpid():
            raise JournalBusy("Open a new Journal instance after a process fork")
        if not self._gate.acquire(blocking=self._shared):
            raise JournalBusy("Store is locked by another thread")
        try:
            if self._depth == 0:
                self._invalidate_cache()
                fd = _open(self.lock_path, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    os.close(fd)
                    raise JournalBusy("Store is locked by another executor") from exc
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
                self._lock_epoch += 1
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._fd is not None:
                    self._invalidate_cache()
                    fd, self._fd = self._fd, None
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
        finally:
            self._gate.release()

    @contextmanager
    def shared_locked(self) -> Iterator[None]:
        """One process controller, with serialized same-instance worker access.

        Nest the worker executor *inside* this context, so all in-flight work
        joins before the process lock is released. Each ordinary journal method
        still takes the gate; only remote work may overlap. Other Journal
        instances/processes remain excluded by the same existing flock.
        """
        with self.locked():
            if self._shared or self._depth != 1:
                raise JournalBusy("A controller already holds this journal")
            self._shared = True
            # Keep locked()'s depth-one process lock while letting worker
            # methods acquire the gate one at a time. Restore its gate ownership
            # before leaving the outer context, including exceptional exits.
            self._gate.release()
            try:
                yield
            finally:
                self._gate.acquire()
                self._shared = False

    def _complete(self) -> None:
        if (self.root / ".snapshot-incomplete").exists():
            raise JournalCorrupt("Snapshot was interrupted; do not use it to resume")

    def _invalidate_cache(self, *, poisoned: bool = False) -> None:
        self._cached_records = None
        self._cached_stat = None
        self._cache_poisoned = poisoned

    def _journal_stat(self) -> tuple[int, ...]:
        try:
            info = self.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise JournalCorrupt("Journal file is unavailable during lock epoch") from exc
        if not stat.S_ISREG(info.st_mode):
            raise JournalCorrupt("Journal path is no longer a regular file")
        return _fingerprint(info)

    def _check_cache_stat(self) -> None:
        if self._cache_poisoned:
            raise JournalCorrupt("Journal cache invalidated; release and revalidate before continuing")
        if self._cached_stat is not None and self._journal_stat() != self._cached_stat:
            self._invalidate_cache(poisoned=True)
            raise JournalCorrupt("Journal changed unexpectedly during lock epoch")

    def _records_cached(self) -> list[dict[str, Any]]:
        """Internal values must never escape without a deep copy."""
        self._complete()
        try:
            self._check_cache_stat()
            if self._cached_records is None:
                before = self._journal_stat()
                raw = _read(self.path)
                records = _validate(raw)
                after = self._journal_stat()
                if before != after or len(raw) != after[2]:
                    raise JournalCorrupt("Journal changed during full validation")
                self._cached_records, self._cached_stat = records, after
            return self._cached_records
        except BaseException:
            self._invalidate_cache(poisoned=True)
            raise

    def records(self) -> list[dict[str, Any]]:
        with self.locked():
            return copy.deepcopy(self._records_cached())

    def records_after(self, sequence: int) -> list[dict[str, Any]]:
        """Return an isolated ordered tail with seq greater than sequence."""
        if type(sequence) is not int or sequence < 0:
            raise ValueError("Sequence must be a nonnegative integer")
        with self.locked():
            records = self._records_cached()
            if sequence > len(records):
                raise JournalCorrupt("Journal cursor is ahead of the verified chain")
            return copy.deepcopy(records[sequence:])

    def append(self, kind: str, operation_id: str | None = None, attempt: int = 0,
               payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.locked():
            try:
                records = self._records_cached()
                before = self._cached_stat
                record = {
                    "schema_version": 1,
                    "seq": len(records) + 1,
                    "event_id": str(uuid.uuid4()),
                    "at": _now(),
                    "kind": kind,
                    "operation_id": operation_id,
                    "attempt": attempt,
                    "payload": {} if payload is None else payload,
                    "prev_hash": records[-1]["hash"] if records else GENESIS,
                }
                record["hash"] = digest(canonical(record))
                encoded = canonical(record) + b"\n"
                # Only the new entry needs validation inside an unchanged lock
                # epoch. The full existing chain was verified on first access.
                saved = _validate_record(encoded[:-1], len(records) + 1, record["prev_hash"])
                fd = _open(self.path, os.O_RDWR | os.O_APPEND)
                try:
                    if _fingerprint(os.fstat(fd)) != before or self._journal_stat() != before:
                        raise JournalCorrupt("Journal changed before append")
                    _write_all(fd, encoded)
                    os.fsync(fd)
                    after = _fingerprint(os.fstat(fd))
                    if after[:2] != before[:2] or after[2] != before[2] + len(encoded):
                        raise JournalCorrupt("Journal append size or identity mismatch")
                    if os.pread(fd, len(encoded), before[2]) != encoded:
                        raise JournalCorrupt("Journal append readback did not match")
                    if _fingerprint(os.fstat(fd)) != after or self._journal_stat() != after:
                        raise JournalCorrupt("Journal changed during append readback")
                finally:
                    os.close(fd)
                records.append(saved)
                self._cached_stat = after
                return copy.deepcopy(saved)
            except BaseException:
                # A failed write/fsync/readback may already be on disk. Never
                # advance the memory tip; reacquire and validate it afresh.
                self._invalidate_cache(poisoned=True)
                raise

    def evidence(self, data: Any) -> str:
        with self.locked():
            self._complete()
            encoded = canonical(data)
            content_hash = digest(encoded)
            path = self.evidence_dir / f"{content_hash}.json"
            if path.exists() or path.is_symlink():
                if _read(path) != encoded:
                    raise JournalCorrupt("Existing content-addressed evidence is corrupt")
            else:
                pending = self.pending_dir / f"{uuid.uuid4()}.json"
                _new_file(pending, encoded)
                try:
                    # link() is an atomic, no-overwrite publication of the
                    # already fsynced file within this local filesystem.
                    os.link(pending, path)
                except FileExistsError:
                    if _read(path) != encoded:
                        raise JournalCorrupt("Existing content-addressed evidence is corrupt")
                _fsync_dir(self.evidence_dir)
                if _read(path) != encoded:
                    raise JournalCorrupt("Evidence publication readback failed")
                pending.unlink()
                _fsync_dir(self.pending_dir)
            reference = f"sha256:{content_hash}"
            if canonical(self.read_evidence(reference)) != encoded:
                raise JournalCorrupt("Evidence readback did not match")
            return reference

    def read_evidence(self, reference: str) -> Any:
        with self.locked():
            self._complete()
            if not isinstance(reference, str) or not reference.startswith("sha256:"):
                raise JournalCorrupt("Invalid evidence reference")
            content_hash = reference[7:]
            if not _DIGEST.fullmatch(content_hash):
                raise JournalCorrupt("Invalid evidence digest")
            path = self.evidence_dir / f"{content_hash}.json"
            try:
                raw = _read(path)
                if digest(raw) != content_hash:
                    raise JournalCorrupt("Evidence content hash mismatch")
                result = json.loads(raw)
                if canonical(result) != raw:
                    raise JournalCorrupt("Evidence is not canonical JSON")
                return result
            except (FileNotFoundError, ValueError, UnicodeDecodeError) as exc:
                raise JournalCorrupt("Missing or invalid evidence") from exc

    def _all_evidence(self) -> dict[str, bytes]:
        result = {}
        for path in sorted(self.evidence_dir.iterdir()):
            if path.suffix != ".json" or not _DIGEST.fullmatch(path.stem):
                raise JournalCorrupt("Unexpected file in content-addressed evidence store")
            self.read_evidence(f"sha256:{path.stem}")
            result[path.name] = _read(path)
        return result

    def _snapshot(self, destination: str | Path, *, recover: bool) -> dict[str, Any]:
        with self.locked():
            self._complete()
            destination = Path(destination).absolute()
            if destination == self.root:
                raise ValueError("Snapshot destination must be a NEW directory")
            self._check_cache_stat()
            source = _read(self.path)
            tail = b""
            prefix = source
            if recover and source and not source.endswith(b"\n"):
                boundary = source.rfind(b"\n") + 1
                prefix, tail = source[:boundary], source[boundary:]
            records = _validate(prefix)
            evidence = self._all_evidence()
            # A missing referenced evidence file blocks every backup/recovery.
            def verify_refs(value: Any) -> None:
                if isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                    self.read_evidence(value)
                elif isinstance(value, dict):
                    for child in value.values():
                        verify_refs(child)
                elif isinstance(value, list):
                    for child in value:
                        verify_refs(child)
            for record in records:
                verify_refs(record["payload"])
            destination.mkdir(mode=0o700, parents=False, exist_ok=False)
            _fsync_dir(destination.parent)
            _new_file(destination / ".snapshot-incomplete", b"Incomplete snapshot: do not resume\n")
            (destination / "evidence").mkdir(mode=0o700)
            _fsync_dir(destination)
            _new_file(destination / "events.jsonl", prefix)
            _new_file(destination / ".lock", b"")
            for name, content in evidence.items():
                _new_file(destination / "evidence" / name, content)
            manifest = {
                "schema_version": 1,
                "kind": "recovery" if recover else "backup",
                "created_at": _now(),
                "source_path": str(self.root),
                "source_journal_sha256": digest(source),
                "copied_journal_sha256": digest(prefix),
                "event_count": len(records),
                "head_hash": records[-1]["hash"] if records else GENESIS,
                "evidence_sha256": {name: digest(data) for name, data in evidence.items()},
                "discarded_terminal_byte_count": len(tail),
                "discarded_terminal_sha256": digest(tail) if tail else None,
                "reconciliation_required": bool(tail),
                "pending_evidence_excluded": len(list(self.pending_dir.iterdir())),
            }
            if recover:
                (destination / "forensic").mkdir(mode=0o700)
                _new_file(destination / "forensic" / "source-events.jsonl", source)
            manifest["manifest_sha256"] = digest(canonical(manifest))
            _new_file(destination / "snapshot-manifest.json", canonical(manifest))
            if _validate(_read(destination / "events.jsonl")) != records:
                raise JournalCorrupt("Snapshot journal verification failed")
            for name, content in evidence.items():
                if _read(destination / "evidence" / name) != content:
                    raise JournalCorrupt("Snapshot evidence verification failed")
            (destination / ".snapshot-incomplete").unlink()
            _fsync_dir(destination)
            _fsync_dir(destination.parent)
            return manifest

    def backup(self, destination: str | Path) -> dict[str, Any]:
        """Create a verified, consistent local snapshot; never overwrite a backup."""
        return self._snapshot(destination, recover=False)

    def recover_to(self, destination: str | Path) -> dict[str, Any]:
        """Salvage only a torn terminal record into a NEW store.

        The source remains byte-for-byte untouched. Interior corruption is never
        salvaged. Any dispatched operations still require live reconciliation:
        a discarded tail may have been a result recorded after remote success.
        """
        return self._snapshot(destination, recover=True)
