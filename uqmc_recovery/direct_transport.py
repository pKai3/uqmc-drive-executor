"""Small direct Google Drive transport; authorization setup is deliberately separate.

This module does not discover credentials, start OAuth, save refreshed tokens,
or journal operations. The caller owns durable intent/readback and the writer
lock. A returned HTTP error or timeout must never be treated as proof that a
metadata mutation did not happen.

Official references:
https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update
https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list
https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions/list
https://developers.google.com/workspace/drive/api/guides/handle-errors
https://googleapis.dev/python/google-auth-httplib2/latest/google_auth_httplib2.html
"""
from __future__ import annotations

import copy
import errno
import http.client
import logging
import random
import time
import json
from pathlib import Path
from urllib.parse import quote, quote_plus
import re
import stat

from .model import SafetyError

FULL_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata"
READONLY_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
DEFAULT_REQUEST_TIMEOUT = 15
READ_MAX_ATTEMPTS = 3
_RETRYABLE_READS = frozenset({'files.get', 'files.list', 'about.get', 'permissions.list'})
_LOG = logging.getLogger(__name__)
FILE_FIELDS = (
    "id,name,mimeType,parents,modifiedTime,version,trashed,size,md5Checksum,"
    "owners,driveId,capabilities,permissions,shortcutDetails,appProperties,"
    "inheritedPermissionsDisabled,shared,hasAugmentedPermissions"
)
# Explicitly accepted no-audit mode still reads fields used for identity,
# content/concurrency and mutation-capability checks. It neither fetches nor
# changes sharing. The default field selection remains unchanged.
NO_PERMISSION_AUDIT_FIELDS = (
    "id,name,mimeType,parents,modifiedTime,version,trashed,size,md5Checksum,"
    "owners,driveId,shortcutDetails,appProperties,"
    "capabilities(canRename,canAddChildren,canMoveItemWithinDrive,canMoveItemOutOfDrive)"
)
# Membership and collision checks use only these fields. Full source and
# parent reads retain FILE_FIELDS and separate paginated permission reads.
CHILD_FIELDS = "id,name,mimeType,parents"
_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


class _ReplayBlocked(RuntimeError):
    pass


def _single_send_http_class(base):
    """Fence write replay; allow one identical GET/HEAD stale-socket retry.

    httplib2 retries BadStatusLine/ResponseNotReady even with RETRIES=1.
    Mutations still permit exactly one send per outer request. Safe reads may
    send twice, with the existing socket timeout on each attempt. This is not
    a wall-clock deadline. Each worker owns its HTTP client.
    """
    class Connection:
        def __init__(self, wrapped, owner):
            self.wrapped, self.owner = wrapped, owner

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def request(self, *args, **kwargs):
            method = args[0] if args else kwargs.get("method")
            uri = args[1] if len(args) > 1 else kwargs.get("url")
            signature = (method, uri)
            if self.owner._send_count:
                if (method not in {"GET", "HEAD"} or self.owner._send_count >= 2
                        or signature != self.owner._first_request):
                    raise _ReplayBlocked("AUTOMATIC_HTTP_REPLAY_BLOCKED")
            else:
                self.owner._first_request = signature
            self.owner._send_count += 1
            return self.wrapped.request(*args, **kwargs)

    class SingleSendHttp(base):
        def request(self, *args, **kwargs):
            self._send_count = 0
            self._first_request = None
            return super().request(*args, **kwargs)

        def _conn_request(self, conn, *args, **kwargs):
            return super()._conn_request(Connection(conn, self), *args, **kwargs)

    return SingleSendHttp


class TransportError(SafetyError):
    """Safe code plus bounded, redacted structured provider diagnostics."""

    def __init__(self, code, *, status=None, outcome="NOT_DISPATCHED", request=None, item_id=None, provider_error=None, read_attempts=None):
        self.code, self.status, self.outcome = code, status, outcome
        self.request, self.item_id = request, item_id
        self.provider_error = copy.deepcopy(provider_error)
        self.read_attempts = read_attempts
        context = (":" + request if request else "") + (":" + item_id if item_id else "")
        super().__init__(code + (f":HTTP_{status}" if status is not None else "") + context)

    def as_dict(self):
        result = {"code": self.code, "status": self.status, "outcome": self.outcome}
        if self.request is not None:
            result["request"] = self.request
        if self.item_id is not None:
            result["item_id"] = self.item_id
        if self.read_attempts is not None:
            result["read_attempts"] = self.read_attempts
        if self.provider_error:
            result["provider_error"] = copy.deepcopy(self.provider_error)
        return result

    @property
    def conflict(self):
        return self.status == 412

    @property
    def transient(self):
        # Classification is advice for a fresh-read recovery loop, never a
        # transport-level instruction to replay a mutation.
        return (self.status == 403 and self.code == "DRIVE_RATE_LIMIT") or self.status in {408, 429, 500, 502, 503, 504} or (
            self.status is None and self.code in {"DRIVE_TRANSPORT_ERROR", "DRIVE_REQUEST_TIMEOUT"})


def _is_timeout(error):
    """Recognize socket timeout and auth wrappers without inspecting messages."""
    pending, seen = [error], set()
    while pending and len(seen) < 16:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, TimeoutError):
            return True
        pending.extend(x for x in (getattr(current, "__cause__", None),
            getattr(current, "__context__", None), *getattr(current, "args", ()))
            if isinstance(x, BaseException))
    return False



def _transient_read(error, failure):
    if failure.status is not None:
        return failure.transient
    if _is_timeout(error) or isinstance(error, (ConnectionError, http.client.BadStatusLine,
                                               http.client.IncompleteRead, http.client.ResponseNotReady)):
        return True
    if isinstance(error, OSError) and error.errno in {
            errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED,
            errno.EPIPE, errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH}:
        return True
    try:
        from httplib2 import ServerNotFoundError
    except ImportError:
        return False
    return isinstance(error, ServerNotFoundError)


def _is_rate_limit_403(error, status):
    """Allow only documented structured rate-limit reasons, never message text."""
    if status != 403:
        return False
    content = getattr(error, "content", None)
    if not isinstance(content, (bytes, str)) or len(content) > 65536:
        return False
    try:
        body = json.loads(content)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    details = body.get("error") if isinstance(body, dict) else None
    errors = details.get("errors") if isinstance(details, dict) else None
    # Mixed or unknown reasons can include permission failures; fail closed.
    return isinstance(errors, list) and bool(errors) and all(
        isinstance(entry, dict) and isinstance(entry.get("reason"), str)
        and entry["reason"] in {"rateLimitExceeded", "userRateLimitExceeded"}
        for entry in errors
    )


def _provider_error(error, secret_values):
    """Retain only structured reasons/message, never headers, URI or repr.

    Known in-memory credentials and common credential/URL forms are redacted
    before truncation. Diagnostics stay in private local logs/journal.
    """
    content = getattr(error, "content", None)
    if not isinstance(content, (str, bytes)) or len(content) > 65536:
        return None
    try:
        body = json.loads(content)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    details = body.get("error") if isinstance(body, dict) else None
    if not isinstance(details, dict):
        return None
    try:
        values = secret_values() if callable(secret_values) else secret_values
        secrets = {variant for value in values if isinstance(value, str) and value
                   for variant in (value, quote(value, safe=""), quote_plus(value))}
    except Exception:
        return None  # Never emit message text if credential redaction failed.
    def redact(value):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"https?://[^\s<>\"']+", "[URL REDACTED]", value, flags=re.I)
        value = re.sub(r"\b(?:Bearer|Basic)\s+[^\s,;\"']+", "[AUTH REDACTED]", value, flags=re.I)
        value = re.sub(r"(?i)(?:access_token|refresh_token|id_token|client_secret|client_id|api[_ -]?key|password|authorization)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", "[CREDENTIAL REDACTED]", value)
        value = re.sub(r"\b(?:ya29\.|GOCSPX-|1//)[A-Za-z0-9_./+=-]+", "[TOKEN REDACTED]", value)
        value = re.sub(r"[A-Za-z0-9_./+=-]{32,}", "[OPAQUE VALUE REDACTED]", value)
        return " ".join(value.split())[:1024]
    errors = details.get("errors", [])
    errors = errors[:20] if isinstance(errors, list) else []
    reasons = []
    for entry in errors:
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if isinstance(reason, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", reason) and not any(v in reason for v in secrets):
            if reason not in reasons:
                reasons.append(reason)
    message = details.get("message")
    if not isinstance(message, str):
        message = next((x.get("message") for x in errors if isinstance(x, dict) and isinstance(x.get("message"), str)), None)
    result = {"reasons": reasons} if reasons else {}
    if message:
        result["message"] = redact(message)
    return result or None


def _failure(error, *, dispatched, request=None, item_id=None, secret_values=()):
    status = getattr(getattr(error, "resp", None), "status", None)
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        status = None
    # Even an HTTP refusal is left to the recovery layer, not auto-retried.
    return TransportError(
        "AUTOMATIC_HTTP_REPLAY_BLOCKED" if isinstance(error, _ReplayBlocked)
        else "DRIVE_REQUEST_TIMEOUT" if _is_timeout(error)
        else "DRIVE_RATE_LIMIT" if _is_rate_limit_403(error, status)
        else "DRIVE_HTTP_ERROR" if status else "DRIVE_TRANSPORT_ERROR",
        status=status, outcome="UNKNOWN" if dispatched else "READ_FAILED",
        request=request, item_id=item_id, provider_error=_provider_error(error, secret_values)
    )


class GoogleDriveTransport:
    """Only exact-ID reads and metadata-only rename/reparent calls are exposed."""

    def __init__(self, service, *, apply=False, allowed_ids, excluded_ids=(), mutation_ids=None,
                 refresh_credentials=None, audit_permissions=True, secret_values=None,
                 creation_parent_ids=()):
        if type(apply) is not bool:
            raise TransportError("EXPLICIT_APPLY_BOOLEAN_REQUIRED")
        if type(audit_permissions) is not bool:
            raise TransportError("EXPLICIT_PERMISSION_AUDIT_BOOLEAN_REQUIRED")
        self.service = service
        self.apply_enabled = apply
        self.audit_permissions = audit_permissions
        self.file_fields = FILE_FIELDS if audit_permissions else NO_PERMISSION_AUDIT_FIELDS
        self.allowed_ids = frozenset(allowed_ids)
        self.mutation_ids = self.allowed_ids if mutation_ids is None else frozenset(mutation_ids)
        self.excluded_ids = frozenset(excluded_ids)
        if not self.allowed_ids or any(not self._valid_id(x) for x in self.allowed_ids | self.excluded_ids):
            raise TransportError("VALID_EXACT_ID_ALLOWLIST_REQUIRED")
        if not self.mutation_ids <= self.allowed_ids:
            raise TransportError("MUTATION_IDS_MUST_BE_IN_READ_SCOPE")
        self.creation_parent_ids = frozenset(creation_parent_ids)
        if not self.creation_parent_ids <= self.allowed_ids - self.excluded_ids:
            raise TransportError("CREATION_PARENT_OUTSIDE_FROZEN_SCOPE")
        self._reserved_folder_ids = {}
        self._refresh_credentials = refresh_credentials or (lambda: None)
        self._secret_values = secret_values or (lambda: ())

    @staticmethod
    def _valid_id(value):
        return isinstance(value, str) and value != "root" and _ID.fullmatch(value) is not None

    def _guard(self, value):
        if not self._valid_id(value):
            raise TransportError("STABLE_FILE_ID_REQUIRED")
        if value in self.excluded_ids:
            raise TransportError("EXCLUDED")
        if value not in self.allowed_ids:
            raise TransportError("OUTSIDE_FROZEN_SCOPE")
        return value

    def _ready(self, *, item_id=None):
        # Refresh happens before any resource request. Retry only transient
        # authentication transport/provider failures; never replay a file write.
        for attempt in range(1, READ_MAX_ATTEMPTS + 1):
            try:
                self._refresh_credentials()
                return
            except TransportError as error:
                failure = error
                transient = error.transient or error.code in {'AUTH_REFRESH_RETRY_REQUIRED', 'AUTH_REQUEST_TIMEOUT'}
                if failure.request is None: failure.request = 'oauth.refresh'
                if failure.item_id is None: failure.item_id = item_id
            except Exception as error:
                wrapped, seen = [error], set()
                transient = bool(getattr(error, 'retryable', False))
                while wrapped and len(seen) < 16:
                    cause = wrapped.pop()
                    if id(cause) in seen:
                        continue
                    seen.add(id(cause))
                    transient |= _transient_read(cause, _failure(cause, dispatched=False))
                    transient |= isinstance(cause, _ReplayBlocked)
                    wrapped.extend(x for x in (getattr(cause, '__cause__', None),
                        getattr(cause, '__context__', None), *getattr(cause, 'args', ()))
                        if isinstance(x, BaseException))
                code = 'AUTH_REQUEST_TIMEOUT' if _is_timeout(error) else 'AUTH_REFRESH_FAILED'
                details = {'error_class': type(error).__name__}
                for value in getattr(error, 'args', ()):
                    if isinstance(value, dict) and value.get('error') in {
                            'invalid_grant', 'invalid_client', 'unauthorized_client',
                            'temporarily_unavailable', 'server_error'}:
                        details['oauth_error'] = value['error']
                failure = TransportError(code, request='oauth.refresh', item_id=item_id,
                    provider_error=details)
            failure.read_attempts = attempt
            if not transient or attempt == READ_MAX_ATTEMPTS:
                raise failure from None
            delay = random.uniform(2 ** (attempt - 1), 2 ** attempt)
            _LOG.warning('Retrying oauth.refresh after %s; attempt %d/%d in %.2fs',
                failure.code, attempt + 1, READ_MAX_ATTEMPTS, delay)
            time.sleep(delay)

    def _read(self, factory, *, request, item_id=None):
        # Authentication/factory errors and unlisted operations never enter the
        # safe-read backoff loop. Writes still have their separate single send.
        self._ready(item_id=item_id)
        managed = request in _RETRYABLE_READS
        attempts = READ_MAX_ATTEMPTS if managed else 1
        for attempt in range(1, attempts + 1):
            call = factory()
            try:
                result = call.execute(num_retries=0)
            except Exception as error:
                failure = _failure(error, dispatched=False, request=request,
                    item_id=item_id, secret_values=self._secret_values)
                if managed:
                    # Outer preflight/runner loops must not multiply this budget,
                    # including when this policy rejects a non-network failure.
                    failure.read_attempts = attempt
                if attempt == attempts or not _transient_read(error, failure):
                    raise failure from None
                delay = random.uniform(2 ** (attempt - 1), 2 ** attempt)
                _LOG.warning('Retrying %s%s after %s; attempt %d/%d in %.2fs',
                    request, ':' + item_id if item_id else '', failure.code,
                    attempt + 1, attempts, delay)
                time.sleep(delay)
                continue
            if not isinstance(result, dict):
                raise TransportError('INVALID_DRIVE_RESPONSE', outcome='READ_FAILED')
            return result

    def get_file(self, file_id):
        file_id = self._guard(file_id)
        response_headers = {}
        def request():
            response_headers.clear()  # Never retain an ETag from a failed read.
            value = self.service.files().get(
                fileId=file_id, fields=self.file_fields, supportsAllDrives=True)
            if hasattr(value, "add_response_callback"):
                value.add_response_callback(lambda headers: response_headers.update(headers))
            return value
        result = self._read(request, request="files.get", item_id=file_id)
        if result.get("id") != file_id:
            raise TransportError("DRIVE_RESPONSE_ID_MISMATCH", outcome="READ_FAILED")
        # Drive may omit an empty appProperties map. Preserve that we actually
        # requested it, so cache recovery can distinguish absence from omission.
        result["_app_properties_requested"] = True
        # This is an HTTP response header, not a synthetic File resource field.
        # The Python client exposes response callbacks; Drive returning and
        # enforcing an ETag for this account still needs a live integration test.
        etag = next((value for key, value in response_headers.items()
                     if key.lower() == "etag"), None)
        if etag is not None:
            result["_http_etag"] = etag
        return copy.deepcopy(result)

    def get_account(self):
        result = self._read(lambda: self.service.about().get(
            fields="user(displayName,emailAddress,permissionId,me)"), request="about.get")
        user = result.get("user", {})
        if user.get("me") is not True or not user.get("emailAddress"):
            raise TransportError("AUTHENTICATED_ACCOUNT_UNVERIFIED", outcome="READ_FAILED")
        return {key: user.get(key) for key in ("displayName", "emailAddress", "permissionId")}

    def _pages(self, factory, key, *, request, item_id):
        token = None
        seen_tokens, seen_ids, items = set(), set(), []
        for _ in range(1000):
            page = self._read(lambda: factory(token), request=request, item_id=item_id)
            if page.get("incompleteSearch") is True:
                raise TransportError("INCOMPLETE_DRIVE_SEARCH", outcome="READ_FAILED")
            values = page.get(key)
            if not isinstance(values, list):
                raise TransportError("INVALID_DRIVE_LIST_RESPONSE", outcome="READ_FAILED")
            for value in values:
                if not isinstance(value, dict) or not self._valid_id(value.get("id")):
                    raise TransportError("INVALID_DRIVE_LIST_ITEM", outcome="READ_FAILED")
                if value["id"] in seen_ids:
                    raise TransportError("DUPLICATE_DRIVE_LIST_ID", outcome="READ_FAILED")
                seen_ids.add(value["id"])
                items.append(copy.deepcopy(value))
            token = page.get("nextPageToken")
            if token is None or token == "":
                return items
            if not isinstance(token, str) or token in seen_tokens:
                raise TransportError("INVALID_DRIVE_PAGE_TOKEN", outcome="READ_FAILED")
            seen_tokens.add(token)
        raise TransportError("DRIVE_PAGINATION_LIMIT", outcome="READ_FAILED")

    def list_children(self, parent_id):
        parent_id = self._guard(parent_id)
        # Stable-ID validation excludes quote/query injection. Listing a parent
        # can return out-of-scope children; no follow-up requests are made.
        return self._pages(lambda token: self.service.files().list(
            q=f"'{parent_id}' in parents and trashed = false",
            pageSize=1000, pageToken=token, spaces="drive",
            fields=f"nextPageToken,incompleteSearch,files({CHILD_FIELDS})",
            supportsAllDrives=True, includeItemsFromAllDrives=True), "files", request="files.list", item_id=parent_id)

    def list_permissions(self, file_id):
        file_id = self._guard(file_id)
        if not self.audit_permissions:
            raise TransportError("PERMISSION_AUDIT_DISABLED")
        return self._pages(lambda token: self.service.permissions().list(
            fileId=file_id, pageSize=100, pageToken=token,
            fields="nextPageToken,permissions", supportsAllDrives=True), "permissions", request="permissions.list", item_id=file_id)

    def reserve_folder_id(self, parent_id):
        """Allocate an ID, without creating a Drive object. Caller journals it."""
        self._creation_guard(parent_id)
        result = self._read(lambda: self.service.files().generateIds(
            count=1, space="drive", type="files"), request="files.generateIds", item_id=parent_id)
        ids = result.get("ids")
        if not isinstance(ids, list) or len(ids) != 1 or not self._valid_id(ids[0]):
            raise TransportError("GENERATED_FOLDER_ID_INVALID")
        if ids[0] in self.allowed_ids or ids[0] in self.excluded_ids:
            raise TransportError("GENERATED_FOLDER_ID_COLLISION")
        self.register_reserved_folder_id(ids[0], parent_id)
        return ids[0]

    def _creation_guard(self, parent_id):
        if not self.apply_enabled:
            raise TransportError("READ_ONLY_TRANSPORT")
        self._guard(parent_id)
        if parent_id not in self.creation_parent_ids:
            raise TransportError("OUTSIDE_APPROVED_CREATION_PARENTS")

    def register_reserved_folder_id(self, file_id, parent_id):
        """Restore a WAL-bound allocation; does not authorize ordinary updates."""
        self._creation_guard(parent_id)
        if not self._valid_id(file_id) or file_id in self.excluded_ids or file_id == parent_id:
            raise TransportError("INVALID_RESERVED_FOLDER_ID")
        old = self._reserved_folder_ids.get(file_id)
        if old is not None and old != parent_id:
            raise TransportError("RESERVED_FOLDER_PARENT_MISMATCH")
        self._reserved_folder_ids[file_id] = parent_id
        self.allowed_ids = self.allowed_ids | {file_id}

    def get_reserved_folder(self, file_id):
        if file_id not in self._reserved_folder_ids:
            raise TransportError("UNRESERVED_FOLDER_ID")
        result = self._read(lambda: self.service.files().get(
            fileId=self._guard(file_id), fields=self.file_fields,
            supportsAllDrives=True), request="files.get", item_id=file_id)
        if result.get("id") != file_id:
            raise TransportError("DRIVE_RESPONSE_ID_MISMATCH", outcome="READ_FAILED")
        return copy.deepcopy(result)

    def create_reserved_folder(self, file_id, *, name, parent_id, stable_key):
        """One metadata-only create using a preallocated ID; never auto-replay.

        Google documents pre-generated IDs for folders at
        https://developers.google.com/workspace/drive/api/guides/create-file
        """
        self._creation_guard(parent_id)
        if self._reserved_folder_ids.get(file_id) != parent_id:
            raise TransportError("UNRESERVED_FOLDER_ID")
        if not isinstance(name, str) or not name or "\x00" in name:
            raise TransportError("EXACT_NONEMPTY_NAME_REQUIRED")
        if not isinstance(stable_key, str) or not re.fullmatch(r"[a-f0-9]{64}", stable_key):
            raise TransportError("EXACT_FOLDER_STABLE_KEY_REQUIRED")
        self._ready(item_id=file_id)
        try:
            result = self.service.files().create(body={
                "id": file_id, "name": name, "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id], "appProperties": {"uqmcFolderKey": stable_key}},
                fields=self.file_fields, supportsAllDrives=True,
                ignoreDefaultVisibility=True).execute(num_retries=0)
        except Exception as error:
            raise _failure(error, dispatched=True, request="files.create", item_id=file_id,
                           secret_values=self._secret_values) from None
        if not isinstance(result, dict) or result.get("id") != file_id:
            raise TransportError("DRIVE_WRITE_RESPONSE_UNPROVEN", outcome="UNKNOWN")
        return copy.deepcopy(result)

    def get_trash_candidate(self, file_id):
        """Read only the exact identity and trash capability needed for cleanup."""
        file_id = self._guard(file_id)
        return self._read(lambda: self.service.files().get(fileId=file_id,
            fields="id,name,mimeType,parents,trashed,version,capabilities(canTrash)",
            supportsAllDrives=True), request="files.get", item_id=file_id)

    def trash_file(self, file_id):
        """One explicitly scoped recoverable trash request; never permanent delete."""
        if not self.apply_enabled:
            raise TransportError("READ_ONLY_TRANSPORT")
        file_id = self._guard(file_id)
        if file_id not in self.mutation_ids:
            raise TransportError("OUTSIDE_APPROVED_MUTATION_IDS")
        self._ready(item_id=file_id)
        try:
            result = self.service.files().update(fileId=file_id, body={"trashed": True},
                fields="id,name,mimeType,parents,trashed,version", supportsAllDrives=True).execute(num_retries=0)
        except Exception as error:
            raise _failure(error, dispatched=True, request="files.update", item_id=file_id,
                           secret_values=self._secret_values) from None
        if not isinstance(result, dict) or result.get("id") != file_id:
            raise TransportError("DRIVE_WRITE_RESPONSE_UNPROVEN", outcome="UNKNOWN")
        return copy.deepcopy(result)

    def update_file(self, args):
        """Legacy bridge entry point: one effect per call, kept compatible."""
        return self._update(args, combined=False)

    def update_metadata(self, args, *, etag=None):
        """Rename and/or reparent in one metadata PATCH, without hidden retry.

        When supplied, pass the exact observed ETag through If-Match. Absence
        of an ETag must remain visible to the caller; this method does not
        claim an atomic concurrency guarantee or manufacture one from version.
        """
        return self._update(args, combined=True, etag=etag)

    def _update(self, args, *, combined, etag=None):
        if not self.apply_enabled:
            raise TransportError("READ_ONLY_TRANSPORT")
        if not isinstance(args, dict) or set(args) - {"fileId", "name", "addParents", "removeParents"}:
            raise TransportError("METADATA_ONLY_ARGUMENTS_REQUIRED")
        file_id = self._guard(args.get("fileId"))
        if file_id not in self.mutation_ids:
            raise TransportError("OUTSIDE_APPROVED_MUTATION_IDS")
        rename = "name" in args
        moving = bool({"addParents", "removeParents"} & set(args))
        if not rename and not moving:
            raise TransportError("SEPARATE_MOVE_OR_RENAME_REQUIRED")
        if not combined and rename and moving:
            raise TransportError("SEPARATE_MOVE_OR_RENAME_REQUIRED")
        if etag is not None and (not isinstance(etag, str) or len(etag) > 2048
                or not re.fullmatch(r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"', etag)):
            raise TransportError("EXACT_HTTP_ETAG_REQUIRED")
        parameters = {"fileId": file_id, "fields": self.file_fields, "supportsAllDrives": True, "body": {}}
        if rename:
            if not isinstance(args["name"], str) or not args["name"] or "\x00" in args["name"]:
                raise TransportError("EXACT_NONEMPTY_NAME_REQUIRED")
            parameters["body"] = {"name": args["name"]}
        if moving:
            parents = {}
            for field in ("addParents", "removeParents"):
                if field not in args:
                    continue
                value = args[field]
                if not isinstance(value, str) or not value:
                    raise TransportError("EXACT_PARENT_IDS_REQUIRED")
                ids = value.split(",")
                if len(set(ids)) != len(ids):
                    raise TransportError("DUPLICATE_PARENT_IDS")
                for parent in ids:
                    self._guard(parent)
                    if parent == file_id:
                        raise TransportError("SELF_PARENT")
                parents[field] = set(ids)
                parameters[field] = value
            if parents.get("addParents", set()) & parents.get("removeParents", set()):
                raise TransportError("PARENT_ADD_REMOVE_OVERLAP")
        # Pre-request refresh is separate; never retry the request following a
        # 401 or a socket/server error. The caller journals ATTEMPTED first.
        self._ready(item_id=file_id)
        try:
            request = self.service.files().update(**parameters)
            if etag is not None:
                request.headers["If-Match"] = etag
            result = request.execute(num_retries=0)
        except Exception as error:
            raise _failure(error, dispatched=True, request="files.update", item_id=file_id, secret_values=self._secret_values) from None
        if not isinstance(result, dict) or result.get("id") != file_id:
            raise TransportError("DRIVE_WRITE_RESPONSE_UNPROVEN", outcome="UNKNOWN")
        return copy.deepcopy(result)

    @classmethod
    def from_authorized_user_file(cls, token_path, *, apply=False, allowed_ids,
                                  excluded_ids=(), mutation_ids=None, timeout=DEFAULT_REQUEST_TIMEOUT,
                                  audit_permissions=True, creation_parent_ids=()):
        """Load one explicit pre-existing grant; never run OAuth or persist tokens.

        This function is used only by an operator's later command. No token path
        is inferred from environment variables, a home directory, or ADC.
        The default 15-second httplib2 socket timeout applies to Drive and auth
        refresh requests. It bounds individual blocking network operations,
        not total wall-clock duration while a response continues arriving.
        """
        path = Path(token_path)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise TransportError("TOKEN_FILE_REQUIRES_PRIVATE_PERMISSIONS")
        except OSError:
            raise TransportError("EXPLICIT_AUTHORIZED_TOKEN_FILE_REQUIRED") from None
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise TransportError("BOUNDED_HTTP_TIMEOUT_REQUIRED")
        try:
            import httplib2
            import google_auth_httplib2
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError:
            raise TransportError("INSTALL_DIRECT_OPTIONAL_DEPENDENCIES") from None
        try:
            credentials = Credentials.from_authorized_user_file(str(path))
        except Exception:
            raise TransportError("INVALID_AUTHORIZED_TOKEN_FILE") from None
        declared = set(credentials.granted_scopes or credentials.scopes or ())
        permitted_profiles = ({METADATA_SCOPE}, {FULL_DRIVE_SCOPE})
        if not apply:
            permitted_profiles += ({READONLY_SCOPE},)
        if declared not in permitted_profiles:
            raise TransportError("EXACT_SUPPORTED_DRIVE_SCOPE_REQUIRED")
        if creation_parent_ids and declared != {FULL_DRIVE_SCOPE}:
            raise TransportError("FULL_DRIVE_SCOPE_REQUIRED_FOR_FOLDER_CREATION")
        # httplib2 otherwise has its own socket retry loop below HttpRequest.
        # This dedicated single-writer process uses one attempt at that layer.
        httplib2.RETRIES = 1
        httplib2.debuglevel = 0
        raw_http = _single_send_http_class(httplib2.Http)(timeout=timeout)
        raw_http.follow_redirects = False
        auth_http = google_auth_httplib2.AuthorizedHttp(
            credentials, http=raw_http, refresh_status_codes=(), max_refresh_attempts=0)
        def refresh():
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise TransportError("EXPLICIT_GOOGLE_AUTHORIZATION_REQUIRED")
                # The token endpoint may have been idle for an hour. Use a
                # fresh bounded connection, and prevent SDK retries multiplying
                # our explicit three-attempt refresh budget.
                refresh_http = _single_send_http_class(httplib2.Http)(timeout=timeout)
                refresh_http.follow_redirects = False
                request = google_auth_httplib2.Request(refresh_http)
                sent = False
                def once(*args, **kwargs):
                    nonlocal sent
                    if sent:
                        raise TransportError('AUTH_REFRESH_RETRY_REQUIRED', request='oauth.refresh')
                    sent = True
                    response = request(*args, **kwargs)
                    if response.status in {408, 429, 500, 502, 503, 504}:
                        raise TransportError('AUTH_REFRESH_HTTP_ERROR', status=response.status,
                                             request='oauth.refresh')
                    return response
                try:
                    credentials.refresh(once)
                finally:
                    refresh_http.close()
                if not credentials.valid:
                    raise TransportError("AUTH_REFRESH_FAILED")
            scopes = set(credentials.granted_scopes or credentials.scopes or ())
            if scopes not in permitted_profiles:
                raise TransportError("EXACT_SUPPORTED_DRIVE_SCOPE_REQUIRED")
            if creation_parent_ids and scopes != {FULL_DRIVE_SCOPE}:
                raise TransportError("FULL_DRIVE_SCOPE_REQUIRED_FOR_FOLDER_CREATION")
        try:
            service = build("drive", "v3", http=auth_http,
                            cache_discovery=False, static_discovery=True)
        except Exception:
            raise TransportError("DRIVE_CLIENT_INITIALIZATION_FAILED") from None
        return cls(service, apply=apply, allowed_ids=allowed_ids,
                   excluded_ids=excluded_ids, mutation_ids=mutation_ids, refresh_credentials=refresh,
                   audit_permissions=audit_permissions, creation_parent_ids=creation_parent_ids,
                   secret_values=lambda: (credentials.token, credentials.refresh_token, credentials.client_secret, credentials.client_id))
