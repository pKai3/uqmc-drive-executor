"""Small direct Google Drive transport; authorization setup is deliberately separate.

This module does not discover credentials, start OAuth, save refreshed tokens,
or journal operations. The caller owns durable intent/readback and the writer
lock. A returned HTTP error or timeout must never be treated as proof that a
metadata mutation did not happen.

Official references:
https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update
https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list
https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions/list
https://googleapis.dev/python/google-auth-httplib2/latest/google_auth_httplib2.html
"""
from __future__ import annotations

import copy
from pathlib import Path
import re
import stat

from .model import SafetyError

METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata"
READONLY_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
FILE_FIELDS = (
    "id,name,mimeType,parents,modifiedTime,version,trashed,size,md5Checksum,"
    "owners,driveId,capabilities,permissions,shortcutDetails,"
    "inheritedPermissionsDisabled,shared,hasAugmentedPermissions"
)
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

    def __init__(self, code, *, status=None, outcome="NOT_DISPATCHED"):
        self.code, self.status, self.outcome = code, status, outcome
        super().__init__(code + (f":HTTP_{status}" if status is not None else ""))

    def as_dict(self):
        return {"code": self.code, "status": self.status, "outcome": self.outcome}

    @property
    def conflict(self):
        return self.status == 412

    @property
    def transient(self):
        # Classification is advice for a fresh-read recovery loop, never a
        # transport-level instruction to replay a mutation.
        return self.status in {408, 429, 500, 502, 503, 504} or (
            self.status is None and self.code == "DRIVE_TRANSPORT_ERROR")


def _failure(error, *, dispatched):
    status = getattr(getattr(error, "resp", None), "status", None)
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        status = None
    # Even an HTTP refusal is left to the recovery layer, not auto-retried.
    return TransportError(
        "AUTOMATIC_HTTP_REPLAY_BLOCKED" if isinstance(error, _ReplayBlocked)
        else "DRIVE_HTTP_ERROR" if status else "DRIVE_TRANSPORT_ERROR",
        status=status, outcome="UNKNOWN" if dispatched else "READ_FAILED"
    )


class GoogleDriveTransport:
    """Only exact-ID reads and metadata-only rename/reparent calls are exposed."""

    def __init__(self, service, *, apply=False, allowed_ids, excluded_ids=(), mutation_ids=None,
                 refresh_credentials=None):
        if type(apply) is not bool:
            raise TransportError("EXPLICIT_APPLY_BOOLEAN_REQUIRED")
        self.service = service
        self.apply_enabled = apply
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

    def _ready(self):
        try:
            self._refresh_credentials()
        except TransportError:
            raise
        except Exception:
            raise TransportError("AUTH_REFRESH_FAILED") from None

    def _read(self, factory):
        self._ready()
        try:
            result = factory().execute(num_retries=2)
        except Exception as error:
            raise _failure(error, dispatched=False) from None
        if not isinstance(result, dict):
            raise TransportError("INVALID_DRIVE_RESPONSE", outcome="READ_FAILED")
        return result

    def get_file(self, file_id):
        file_id = self._guard(file_id)
        response_headers = {}
        def request():
            value = self.service.files().get(
                fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True)
            if hasattr(value, "add_response_callback"):
                value.add_response_callback(lambda headers: response_headers.update(headers))
            return value
        result = self._read(request)
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
            fields="user(displayName,emailAddress,permissionId,me)"))
        user = result.get("user", {})
        if user.get("me") is not True or not user.get("emailAddress"):
            raise TransportError("AUTHENTICATED_ACCOUNT_UNVERIFIED", outcome="READ_FAILED")
        return {key: user.get(key) for key in ("displayName", "emailAddress", "permissionId")}

    def _pages(self, factory, key):
        token = None
        seen_tokens, seen_ids, items = set(), set(), []
        for _ in range(1000):
            page = self._read(lambda: factory(token))
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
            fields=f"nextPageToken,incompleteSearch,files({FILE_FIELDS})",
            supportsAllDrives=True, includeItemsFromAllDrives=True), "files")

    def list_permissions(self, file_id):
        file_id = self._guard(file_id)
        return self._pages(lambda token: self.service.permissions().list(
            fileId=file_id, pageSize=100, pageToken=token,
            fields="nextPageToken,permissions", supportsAllDrives=True), "permissions")

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
        parameters = {"fileId": file_id, "fields": FILE_FIELDS, "supportsAllDrives": True, "body": {}}
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
        self._ready()
        try:
            request = self.service.files().update(**parameters)
            if etag is not None:
                request.headers["If-Match"] = etag
            result = request.execute(num_retries=0)
        except Exception as error:
            raise _failure(error, dispatched=True) from None
        if not isinstance(result, dict) or result.get("id") != file_id:
            raise TransportError("DRIVE_WRITE_RESPONSE_UNPROVEN", outcome="UNKNOWN")
        return copy.deepcopy(result)

    @classmethod
    def from_authorized_user_file(cls, token_path, *, apply=False, allowed_ids,
                                  excluded_ids=(), mutation_ids=None, timeout=30):
        """Load one explicit pre-existing grant; never run OAuth or persist tokens.

        This function is used only by an operator's later command. No token path
        is inferred from environment variables, a home directory, or ADC.
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
                   excluded_ids=excluded_ids, mutation_ids=mutation_ids, refresh_credentials=refresh)
