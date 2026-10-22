"""Required-folder creation with durable, stable allocation and exact-ID recovery.

No permission calls or deletions. Existing matching names are conflicts, never
silently adopted. A lost create response is reconciled against its preallocated
ID; retries retain that same ID, so they cannot create a second folder identity.
This is not an exactly-once execution guarantee.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import signal
import threading

from .direct_transport import TransportError
from .model import FOLDER, SafetyError, digest


def reject_trailing_whitespace_aliases(children, name, *, context=None):
    """Do not create/adopt a destination beside a whitespace-only near match.

    This guard resolves nothing and renames nothing. Exact IDs/titles must be
    reconciled explicitly before execution; a normalized label is not identity.
    """
    for child in children:
        observed = child.get("name")
        if isinstance(observed, str) and observed != name and observed.rstrip() == name.rstrip():
            raise SafetyError("DESTINATION_TRAILING_WHITESPACE_CONFLICT:"
                              + (context or name) + ":" + str(child.get("id", "UNKNOWN")))


def _authority(journal, reference, key, name, parent_id):
    authority = journal.read_evidence(reference)
    if (authority.get("schema_version") != "uqmc.folder-create-authority.v1"
            or authority.get("decision") != "APPROVED"
            or not authority.get("section_id") or not authority.get("approval_id")
            or authority.get("access_policy") != "SCOPED_INHERITANCE_ACCEPTED"):
        raise SafetyError("EXACT_FOLDER_CREATION_AUTHORITY_REQUIRED")
    provenance = journal.read_evidence(authority.get("provenance_ref"))
    if provenance.get("kind") != "TRUSTED_USER_INSTRUCTION" or not provenance.get("text"):
        raise SafetyError("FOLDER_CREATION_USER_INSTRUCTION_REQUIRED")
    folders = authority.get("folders")
    if not isinstance(folders, list) or len({x.get("key") for x in folders}) != len(folders):
        raise SafetyError("FOLDER_CREATION_SCOPE_INVALID")
    matches = [x for x in folders if x.get("key") == key and x.get("name") == name]
    if len(matches) != 1:
        raise SafetyError("FOLDER_CREATION_OUTSIDE_APPROVED_SCOPE")
    spec = matches[0]
    records = journal.records()
    if any(e["kind"] in {"REVOKED", "SECTION_APPROVAL_REVOKED", "SECTION_APPROVAL_SUPERSEDED"}
           and e["payload"].get("approval_id") == authority["approval_id"] for e in records):
        raise SafetyError("FOLDER_CREATION_AUTHORITY_REVOKED")
    if spec.get("parent_key"):
        expected = [e["payload"].get("file_id") for e in records
                    if e["kind"] == "REQUIRED_FOLDER_VERIFIED"
                    and e["payload"].get("section_id") == authority["section_id"]
                    and e["payload"].get("key") == spec["parent_key"]]
        if not expected or set(expected) != {parent_id}:
            raise SafetyError("FOLDER_PARENT_DEPENDENCY_UNVERIFIED")
    elif spec.get("parent_id") != parent_id:
        raise SafetyError("FOLDER_CREATION_PARENT_SCOPE_MISMATCH")
    return authority, records


def _parent(raw):
    if raw.get("mimeType") != FOLDER or raw.get("trashed") is not False:
        raise SafetyError("FOLDER_CREATION_PARENT_INVALID")
    if raw.get("capabilities", {}).get("canAddChildren") is not True:
        raise SafetyError("FOLDER_CREATION_PARENT_CAPABILITY_UNVERIFIED")
    return {k: raw.get(k) for k in ("id", "name", "mimeType", "parents", "trashed")}


def _verify(raw, file_id, name, parent_id, stable_key):
    if (raw.get("id") != file_id or raw.get("name") != name or raw.get("mimeType") != FOLDER
            or raw.get("parents") != [parent_id] or raw.get("trashed") is not False
            or raw.get("appProperties", {}).get("uqmcFolderKey") != stable_key):
        raise SafetyError("REQUIRED_FOLDER_STATE_CONFLICT")


def ensure_required_folder(journal, transport, *, key, name, parent_id, authority_ref, max_attempts=3):
    """Return verified exact folder metadata, holding the journal writer lock.

    ``authority_ref`` is sealed uqmc.folder-create-authority.v1 evidence with
    section_id, approval_id, decision=APPROVED, provenance_ref to the trusted
    user instruction, access_policy=SCOPED_INHERITANCE_ACCEPTED, and exact
    folders=[{key,name,parent_id}] (or parent_key for a prior created folder).
    Transport must explicitly enable this creation parent. Each invocation
    dispatches at most once. A prior refusal stops; transient/unknown failures
    can resume only using the same allocation after another exact-ID read.
    """
    if not isinstance(key, str) or not key or not isinstance(name, str) or not name or "\x00" in name:
        raise SafetyError("EXACT_REQUIRED_FOLDER_KEY_AND_NAME")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise SafetyError("BOUNDED_FOLDER_ATTEMPTS_REQUIRED")
    with journal.locked():
        return _ensure_required_folder(journal, transport, key=key, name=name, parent_id=parent_id,
                                       authority_ref=authority_ref, max_attempts=max_attempts)


def _ensure_required_folder(journal, transport, *, key, name, parent_id, authority_ref, max_attempts=3):
    """Controller owns the process lock; callers single-flight this stable key."""
    authority, records = _authority(journal, authority_ref, key, name, parent_id)
    section_id = authority["section_id"]
    stable_key = digest({"section_id": section_id, "key": key})
    operation_id = "required-folder-" + stable_key[:24]
    history = [e for e in records if e["operation_id"] == operation_id]
    allocations = [e["payload"] for e in history if e["kind"] == "REQUIRED_FOLDER_ALLOCATED"]
    if len(allocations) > 1:
        raise SafetyError("REQUIRED_FOLDER_ALLOCATION_CONFLICT")
    before_parent = transport.get_file(parent_id)
    parent_core = _parent(before_parent)
    if parent_core["id"] != parent_id:
        raise SafetyError("FOLDER_CREATION_PARENT_ID_MISMATCH")
    if allocations:
        allocation = allocations[0]
        expected = {"section_id": section_id, "key": key, "name": name,
                    "parent_id": parent_id, "stable_key": stable_key}
        if any(allocation.get(k) != v for k, v in expected.items()):
            raise SafetyError("REQUIRED_FOLDER_ALLOCATION_SCOPE_CHANGED")
        file_id = allocation["file_id"]
        transport.register_reserved_folder_id(file_id, parent_id)
    else:
        # List only the selected parent. Do not adopt a coincidentally
        # matching name or create another copy beside an existing folder.
        children = transport.list_children(parent_id)
        reject_trailing_whitespace_aliases(children, name)
        if any(x.get("name") == name for x in children):
            raise SafetyError("REQUIRED_FOLDER_NAME_COLLISION")
        file_id = transport.reserve_folder_id(parent_id)
        journal.append("REQUIRED_FOLDER_ALLOCATED", operation_id, payload={
            "section_id": section_id, "key": key, "name": name, "parent_id": parent_id,
            "stable_key": stable_key, "file_id": file_id, "authority_ref": authority_ref})
    common = {"section_id": section_id, "key": key, "file_id": file_id,
              "authority_ref": authority_ref, "name": name, "parent_id": parent_id}
    try:
        observed = transport.get_reserved_folder(file_id)
    except TransportError as error:
        if error.status != 404:
            raise
    else:
        _verify(observed, file_id, name, parent_id, stable_key)
        journal.append("REQUIRED_FOLDER_VERIFIED", operation_id, payload={**common,
            "observed_ref": journal.evidence(observed), "reconciled": True})
        return observed
    if any(e["kind"] == "REQUIRED_FOLDER_VERIFIED" for e in history):
        raise SafetyError("PREVIOUSLY_CREATED_FOLDER_NOW_UNAVAILABLE")
    failures = [e for e in history if e["kind"] == "REQUIRED_FOLDER_UNCERTAIN"]
    if failures and failures[-1]["payload"].get("retryable") is not True:
        raise SafetyError("REQUIRED_FOLDER_PRIOR_REFUSAL_NEEDS_EXPLICIT_RECOVERY")
    attempt = sum(e["kind"] == "REQUIRED_FOLDER_ATTEMPTED" for e in history) + 1
    if attempt > max_attempts:
        raise SafetyError("REQUIRED_FOLDER_ATTEMPT_LIMIT")
    children = transport.list_children(parent_id)
    reject_trailing_whitespace_aliases(children, name)
    if any(x.get("name") == name and x.get("id") != file_id for x in children):
        raise SafetyError("REQUIRED_FOLDER_NAME_COLLISION")
    current_parent = transport.get_file(parent_id)
    if _parent(current_parent) != parent_core:
        raise SafetyError("FOLDER_CREATION_PARENT_DRIFT")
    with journal.locked():
        _authority(journal, authority_ref, key, name, parent_id)
        journal.append("REQUIRED_FOLDER_PREPARED", operation_id, attempt, payload={**common,
            "parent_before_ref": journal.evidence(current_parent), "stable_key": stable_key})
        journal.append("REQUIRED_FOLDER_ATTEMPTED", operation_id, attempt, payload=common)
    error = None
    try:
        receipt = transport.create_reserved_folder(file_id, name=name, parent_id=parent_id, stable_key=stable_key)
        journal.append("REQUIRED_FOLDER_RECEIPT", operation_id, attempt,
                       payload={**common, "receipt_ref": journal.evidence(receipt)})
    except TransportError as failure:
        error = failure
        journal.append("REQUIRED_FOLDER_UNCERTAIN", operation_id, attempt, payload={**common,
            "error": failure.as_dict(), "retryable": failure.transient or failure.status == 409})
    try:
        observed = transport.get_reserved_folder(file_id)
        _verify(observed, file_id, name, parent_id, stable_key)
    except (TransportError, SafetyError) as failure:
        journal.append("REQUIRED_FOLDER_RECONCILIATION_FAILED", operation_id, attempt,
                       payload={**common, "error_code": str(failure)})
        raise error or failure
    journal.append("REQUIRED_FOLDER_VERIFIED", operation_id, attempt, payload={**common,
        "observed_ref": journal.evidence(observed), "reconciled": error is not None})
    return observed


@contextmanager
def _folder_termination():
    """Request a graceful drain without interrupting a durable journal write."""
    cancelled = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: cancelled.set())
    try:
        yield cancelled
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def prepare_required_folders(journal, factory, *, specs, paths, excluded_ids,
                             authority_ref, workers=10, report=lambda event: None):
    """Create/verify a dependency-ordered forest with bounded remote concurrency.

    The journal process lock spans the entire pool lifetime. Journal methods
    serialize durable evidence/events, while independent HTTP transports overlap.
    A failure or cancellation stops scheduling and joins already dispatched work.
    Re-entry uses the existing stable allocation and reconciles its exact ID.
    """
    if type(workers) is not int or not 1 <= workers <= 32:
        raise SafetyError("FOLDER_WORKERS_OUT_OF_RANGE")
    resolved = dict(paths)
    pending, keys, names = {}, {}, {}
    for original in specs:
        spec = dict(original)
        required = ("logical_path", "logical_key", "folder_name", "parent_path")
        if any(not isinstance(spec.get(k), str) or not spec[k] or "\x00" in spec[k] for k in required):
            raise SafetyError("EXACT_REQUIRED_FOLDER_SPEC_REQUIRED")
        logical, key = spec["logical_path"], spec["logical_key"]
        if logical in pending:
            if pending[logical] != spec:
                raise SafetyError("REQUIRED_FOLDER_LOGICAL_IDENTITY_CONFLICT")
            continue  # The identical request has one future and one allocation.
        if logical in resolved or key in keys:
            raise SafetyError("REQUIRED_FOLDER_LOGICAL_IDENTITY_CONFLICT")
        # Two logical identities must never race to create the same child name.
        # A whitespace-only near match is still a collision, never an alias.
        parent_identity = ("id", resolved[spec["parent_path"]]) if spec["parent_path"] in resolved else ("path", spec["parent_path"])
        name_identity = (parent_identity, spec["folder_name"].rstrip())
        if name_identity in names:
            raise SafetyError("REQUIRED_FOLDER_NAME_COLLISION")
        pending[logical], keys[key], names[name_identity] = spec, logical, logical
    # Prove the graph before any HTTP call, including disconnected/cyclic parents.
    reachable = set(resolved)
    todo = set(pending)
    while todo:
        ready = {path for path in todo if pending[path]["parent_path"] in reachable}
        if not ready:
            raise SafetyError("FOLDER_PARENT_DEPENDENCY_UNRESOLVED")
        reachable.update(ready)
        todo.difference_update(ready)
    if not pending:
        return {}
    key_locks = {key: threading.Lock() for key in keys}
    results, inflight = {}, {}
    failure = None

    def worker(spec, parent_id):
        # Never share a googleapiclient/httplib2 instance across worker threads.
        client = factory({parent_id}, set(), set(excluded_ids), True,
                         creation_parent_ids={parent_id})
        with key_locks[spec["logical_key"]]:
            return _ensure_required_folder(journal, client, key=spec["logical_key"],
                name=spec["folder_name"], parent_id=parent_id, authority_ref=authority_ref)

    with _folder_termination() as cancelled, journal.shared_locked():
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="uqmc-folder") as pool:
            try:
                while pending or inflight:
                    while failure is None and not cancelled.is_set() and len(inflight) < workers:
                        ready = next((path for path, spec in pending.items()
                                      if spec["parent_path"] in resolved), None)
                        if ready is None:
                            break
                        spec = pending.pop(ready)
                        future = pool.submit(worker, spec, resolved[spec["parent_path"]])
                        inflight[future] = spec
                    if not inflight:
                        break
                    done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                    # Consume every completed result before scheduling more work.
                    for future in done:
                        spec = inflight.pop(future)
                        try:
                            raw = future.result()
                        except BaseException as exc:
                            if failure is None:
                                failure = exc
                        else:
                            resolved[spec["logical_path"]] = raw["id"]
                            results[spec["logical_path"]] = raw
                            report({"stage": "folder_verified", "path": spec["logical_path"],
                                    "source_id": raw["id"]})
            except BaseException:
                # The pool context joins any running worker, which records its
                # receipt/readback before the durable writer lock is released.
                for future in inflight:
                    future.cancel()
                raise
    if failure is not None:
        raise failure
    if cancelled.is_set():
        raise KeyboardInterrupt("Folder preparation cancelled after draining in-flight operations")
    return results
