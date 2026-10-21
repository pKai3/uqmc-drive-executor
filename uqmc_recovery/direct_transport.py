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
import json
from pathlib import Path
import re
import stat

from .model import SafetyError

METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata"
READONLY_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
DEFAULT_REQUEST_TIMEOUT = 15
FILE_FIELDS = (
    "id,name,mimeType,parents,modifiedTime,version,trashed,size,md5Checksum,"
    "owners,driveId,capabilities,permissions,shortcutDetails,"
    "inheritedPermissionsDisabled,shared,hasAugmentedPermissions"
)
# Explicitly accepted no-audit mode still reads fields used for identity,
# content/concurrency and mutation-capability checks. It neither fetches nor
# changes sharing. The default field selection remains unchanged.
NO_PERMISSION_AUDIT_FIELDS = (
    "id,name,mimeType,parents,modifiedTime,version,trashed,size,md5Checksum,"
    "owners,driveId,shortcutDetails,"
    "capabilities(canRename,canAddChildren,canMoveItemWithinDrive,canMoveItemOutOfDrive)"
)
# Membership and collision checks use only these fields. Full source and
# parent reads retain FILE_FIELDS and separate paginated permission reads.
CHILD_FIELDS = "id,name,mimeType,parents"
_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


class _ReplayBlocked(RuntimeError):
    pass


def _single_send_http_class(base):
    """Fence httplib2's unconditional BadStatusLine replay below its retry flag.

    The official library retries that case even with RETRIES=1. A connection
    proxy permits one request() call per outer HTTP request; any attempted
    replay fails before another request is sent. Used by this single-writer
    process only, not a shared concurrent HTTP client.
    """
    class Connection:
        def __init__(self, wrapped, owner):
            self.wrapped, self.owner = wrapped, owner

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def request(self, *args, **kwargs):
            if self.owner._send_count:
                raise _ReplayBlocked("AUTOMATIC_HTTP_REPLAY_BLOCKED")
            self.owner._send_count += 1
            return self.wrapped.request(*args, **kwargs)

    class SingleSendHttp(base):
        def request(self, *args, **kwargs):
            self._send_count = 0
            return super().request(*args, **kwargs)

        def _conn_request(self, conn, *args, **kwargs):
            return super()._conn_request(Connection(conn, self), *args, **kwargs)

    return SingleSendHttp


class TransportError(SafetyError):
    """Safe diagnostic only: no provider message, URL, headers or credentials."""

    def __init__(self, code, *, status=None, outcome="NOT_DISPATCHED", request=None, item_id=None):
        self.code, self.status, self.outcome = code, status, outcome
        self.request, self.item_id = request, item_id
        context = (":" + request if request else "") + (":" + item_id if item_id else "")
        super().__init__(code + (f":HTTP_{status}" if status is not None else "") + context)

    def as_dict(self):
        result = {"code": self.code, "status": self.status, "outcome": self.outcome}
        if self.request is not None:
            result["request"] = self.request
        if self.item_id is not None:
            result["item_id"] = self.item_id
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


def _failure(error, *, dispatched, request=None, item_id=None):
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
        request=request, item_id=item_id
    )


class GoogleDriveTransport:
    """Only exact-ID reads and metadata-only rename/reparent calls are exposed."""

    def __init__(self, service, *, apply=False, allowed_ids, excluded_ids=(), mutation_ids=None,
                 refresh_credentials=None, audit_permissions=True):
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
        self._refresh_credentials = refresh_credentials or (lambda: None)

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
        try:
            self._refresh_credentials()
        except TransportError:
            raise
        except Exception as error:
            if _is_timeout(error):
                raise TransportError("AUTH_REQUEST_TIMEOUT", request="oauth.refresh", item_id=item_id) from None
            raise TransportError("AUTH_REFRESH_FAILED") from None

    def _read(self, factory, *, request, item_id=None):
        self._ready(item_id=item_id)
        try:
            # Surface the first timeout/refusal promptly. No hidden read retry
            # or backoff; a caller can make an explicitly bounded fresh read.
            result = factory().execute(num_retries=0)
        except Exception as error:
            raise _failure(error, dispatched=False, request=request, item_id=item_id) from None
        if not isinstance(result, dict):
            raise TransportError("INVALID_DRIVE_RESPONSE", outcome="READ_FAILED")
        return result

    def get_file(self, file_id):
        file_id = self._guard(file_id)
        response_headers = {}
        def request():
            value = self.service.files().get(
                fileId=file_id, fields=self.file_fields, supportsAllDrives=True)
            if hasattr(value, "add_response_callback"):
                value.add_response_callback(lambda headers: response_headers.update(headers))
            return value
        result = self._read(request, request="files.get", item_id=file_id)
        if result.get("id") != file_id:
            raise TransportError("DRIVE_RESPONSE_ID_MISMATCH", outcome="READ_FAILED")
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
            raise _failure(error, dispatched=True, request="files.update", item_id=file_id) from None
        if not isinstance(result, dict) or result.get("id") != file_id:
            raise TransportError("DRIVE_WRITE_RESPONSE_UNPROVEN", outcome="UNKNOWN")
        return copy.deepcopy(result)

    @classmethod
    def from_authorized_user_file(cls, token_path, *, apply=False, allowed_ids,
                                  excluded_ids=(), mutation_ids=None, timeout=DEFAULT_REQUEST_TIMEOUT,
                                  audit_permissions=True):
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
        permitted = {METADATA_SCOPE} if apply else {METADATA_SCOPE, READONLY_SCOPE}
        if not declared or not declared <= permitted or (apply and METADATA_SCOPE not in declared):
            raise TransportError("EXACT_METADATA_SCOPE_REQUIRED")
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
                credentials.refresh(google_auth_httplib2.Request(raw_http))
                if not credentials.valid:
                    raise TransportError("AUTH_REFRESH_FAILED")
            scopes = set(credentials.granted_scopes or credentials.scopes or ())
            if not scopes or not scopes <= permitted or (apply and METADATA_SCOPE not in scopes):
                raise TransportError("EXACT_METADATA_SCOPE_REQUIRED")
        try:
            service = build("drive", "v3", http=auth_http,
                            cache_discovery=False, static_discovery=True)
        except Exception:
            raise TransportError("DRIVE_CLIENT_INITIALIZATION_FAILED") from None
        return cls(service, apply=apply, allowed_ids=allowed_ids,
                   excluded_ids=excluded_ids, mutation_ids=mutation_ids, refresh_credentials=refresh,
                   audit_permissions=audit_permissions)
