"""Combined metadata requests and conditional headers; no credentials/network."""
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from uqmc_recovery.direct_transport import GoogleDriveTransport, TransportError, FILE_FIELDS, CHILD_FIELDS, NO_PERMISSION_AUDIT_FIELDS, METADATA_SCOPE


class Request:
    def __init__(self, service, value):
        self.service, self.value = service, value
        self.headers, self.callbacks = {}, []

    def add_response_callback(self, callback):
        self.callbacks.append(callback)

    def execute(self, *, num_retries):
        self.service.executions.append({"retries": num_retries, "headers": self.headers.copy()})
        for callback in self.callbacks:
            callback(self.service.response_headers)
        if isinstance(self.value, Exception):
            raise self.value
        return copy.deepcopy(self.value)


class Service:
    def __init__(self, *values, headers=None):
        self.values, self.requests, self.executions = list(values), [], []
        self.response_headers = headers or {}

    def files(self):
        return self

    def about(self):
        return self

    def get(self, **kwargs):
        self.requests.append(("get", kwargs))
        return Request(self, self.values.pop(0))

    def list(self, **kwargs):
        self.requests.append(("list", kwargs))
        return Request(self, self.values.pop(0))

    def update(self, **kwargs):
        self.requests.append(("update", kwargs))
        return Request(self, self.values.pop(0))


def transport(*values, apply=True, headers=None, secret_values=None):
    service = Service(*values, headers=headers)
    return GoogleDriveTransport(service, apply=apply,
        allowed_ids={"file-a", "parent-a", "parent-b", "excluded"},
        excluded_ids={"excluded"}, secret_values=secret_values), service


class DesiredTransportTests(unittest.TestCase):
    def setUp(self):
        sleeper=patch('uqmc_recovery.direct_transport.time.sleep')
        sleeper.start();self.addCleanup(sleeper.stop)
        logger=patch('uqmc_recovery.direct_transport._LOG.warning')
        logger.start();self.addCleanup(logger.stop)

    def test_permission_audit_off_uses_narrow_metadata_and_never_lists_grants(self):
        service = Service({"id": "file-a"}, {"id": "file-a"})
        t = GoogleDriveTransport(service, apply=True, allowed_ids={"file-a"}, audit_permissions=False)
        t.get_file("file-a")
        t.update_metadata({"fileId": "file-a", "name": "approved.pdf"})
        self.assertEqual([method for method, _ in service.requests], ["get", "update"])
        for _, args in service.requests:
            self.assertEqual(args["fields"], NO_PERMISSION_AUDIT_FIELDS)
            self.assertNotIn("permissions", args["fields"].lower())
            self.assertIn("capabilities(canRename,canAddChildren,canMoveItemWithinDrive,canMoveItemOutOfDrive)", args["fields"])
            for field in ("id", "name", "mimeType", "parents", "version", "size", "md5Checksum", "owners"):
                self.assertIn(field, args["fields"].split(","))
        with self.assertRaisesRegex(TransportError, "PERMISSION_AUDIT_DISABLED"):
            t.list_permissions("file-a")
        self.assertEqual(len(service.requests), 2)
        default, default_service = transport({"id": "file-a"})
        default.get_file("file-a")
        self.assertTrue(default.audit_permissions)
        self.assertEqual(default_service.requests[0][1]["fields"], FILE_FIELDS)

    def test_authorized_loader_preserves_explicit_permission_audit_mode(self):
        try:
            from google.oauth2.credentials import Credentials
        except ImportError:
            self.skipTest("Optional direct dependencies are not installed")
        credentials = Credentials(token="fixture-only-token", scopes=[METADATA_SCOPE])
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "fixture-token.json"
            token.write_text("fixture only; patched loader")
            token.chmod(0o600)
            with patch.object(Credentials, "from_authorized_user_file", return_value=credentials):
                t = GoogleDriveTransport.from_authorized_user_file(token, apply=False,
                    allowed_ids={"file-a"}, audit_permissions=False)
            self.assertFalse(t.audit_permissions)
            self.assertEqual(t.file_fields, NO_PERMISSION_AUDIT_FIELDS)

    def test_membership_fields_are_narrow_and_all_pages_are_read(self):
        rows = [
            {"id": "child-a", "name": "First.pdf", "mimeType": "application/pdf", "parents": ["parent-a"]},
            {"id": "child-b", "name": "Second", "mimeType": "application/vnd.google-apps.folder", "parents": ["parent-a"]},
        ]
        t, service = transport({"files": rows[:1], "nextPageToken": "page2"}, {"files": rows[1:]})
        self.assertEqual(t.list_children("parent-a"), rows)
        self.assertEqual(CHILD_FIELDS, "id,name,mimeType,parents")
        self.assertEqual(len(service.requests), 2)
        self.assertEqual([args["pageToken"] for _, args in service.requests], [None, "page2"])
        for method, args in service.requests:
            self.assertEqual(method, "list")
            self.assertEqual(args["fields"], "nextPageToken,incompleteSearch,files(id,name,mimeType,parents)")
            self.assertEqual(args["q"], "'parent-a' in parents and trashed = false")
        self.assertEqual([call["retries"] for call in service.executions], [0, 0])

    def test_real_client_loader_wires_15_seconds_with_bounded_reads_and_no_write_replay(self):
        try:
            from google.oauth2.credentials import Credentials
        except ImportError:
            self.skipTest("Optional direct dependencies are not installed")
        credentials = Credentials(token="fixture-only-token", scopes=[METADATA_SCOPE])
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "fixture-token.json"
            token.write_text("fixture only; patched loader")
            token.chmod(0o600)
            with patch.object(Credentials, "from_authorized_user_file", return_value=credentials):
                t = GoogleDriveTransport.from_authorized_user_file(token, apply=True,
                    allowed_ids={"file-a", "parent-a", "parent-b"})
            # These are the installed Google client and AuthorizedHttp objects,
            # with only the network boundary replaced by a synthetic timeout.
            http = t.service._http.http
            self.assertEqual(http.timeout, 15)
            for call, request, item_id, outcome in (
                (t.get_account, "about.get", None, "READ_FAILED"),
                (lambda: t.get_file("file-a"), "files.get", "file-a", "READ_FAILED"),
                (lambda: t.list_children("parent-a"), "files.list", "parent-a", "READ_FAILED"),
                (lambda: t.list_permissions("file-a"), "permissions.list", "file-a", "READ_FAILED"),
                (lambda: t.update_metadata({"fileId": "file-a", "name": "approved.pdf"}), "files.update", "file-a", "UNKNOWN"),
            ):
                with self.subTest(request=request), patch.object(http, "request", side_effect=TimeoutError("private provider text")) as network:
                    with self.assertRaises(TransportError) as caught:
                        call()
                    error = caught.exception
                    self.assertEqual(error.code, "DRIVE_REQUEST_TIMEOUT")
                    self.assertEqual(error.request, request)
                    self.assertEqual(error.item_id, item_id)
                    self.assertEqual(error.outcome, outcome)
                    self.assertTrue(error.transient)
                    self.assertNotIn("private provider text", str(error))
                    self.assertEqual(network.call_count, 1 if request == "files.update" else 3)

    def test_wrapped_auth_timeout_retains_context_without_dispatch(self):
        def refresh():
            error = RuntimeError(TimeoutError("secret refresh payload"))
            raise error from None
        service = Service()
        t = GoogleDriveTransport(service, apply=True, allowed_ids={"file-a"}, refresh_credentials=refresh)
        with self.assertRaises(TransportError) as caught:
            t.update_metadata({"fileId": "file-a", "name": "approved.pdf"})
        self.assertEqual(caught.exception.as_dict(), {"code": "AUTH_REQUEST_TIMEOUT", "status": None,
            "outcome": "NOT_DISPATCHED", "request": "oauth.refresh", "item_id": "file-a",
            "provider_error": {"error_class": "RuntimeError"}, "read_attempts": 3})
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(service.requests, [])

    def test_account_read_is_verified_and_not_hardcoded(self):
        t, service = transport({"user": {"me": True, "emailAddress": "operator@example.test", "displayName": "Operator"}})
        self.assertEqual(t.get_account()["emailAddress"], "operator@example.test")
        self.assertEqual(service.requests[0][1], {"fields": "user(displayName,emailAddress,permissionId,me)"})
        for value in ({"user": {"me": False, "emailAddress": "x@example.test"}}, {"user": {"me": True}}):
            t, _ = transport(value)
            with self.assertRaisesRegex(TransportError, "AUTHENTICATED_ACCOUNT_UNVERIFIED"):
                t.get_account()

    def test_readable_parent_is_not_a_mutation_target(self):
        service = Service({"id": "parent-a"})
        t = GoogleDriveTransport(service, apply=True, allowed_ids={"file-a", "parent-a"}, mutation_ids={"file-a"})
        self.assertEqual(t.get_file("parent-a")["id"], "parent-a")
        with self.assertRaisesRegex(TransportError, "OUTSIDE_APPROVED_MUTATION_IDS"):
            t.update_metadata({"fileId": "parent-a", "name": "unapproved"})
        self.assertEqual(len(service.requests), 1)

    def test_official_client_serializes_combined_patch_and_conditional_header(self):
        try:
            import httplib2
            from googleapiclient.discovery import build
        except ImportError:
            self.skipTest("Optional direct dependencies are not installed")
        class LocalHttp:
            def __init__(self):
                self.calls = []
            def request(self, uri, method="GET", body=None, headers=None, **kwargs):
                self.calls.append({"uri": uri, "method": method, "body": body, "headers": dict(headers or {})})
                return httplib2.Response({"status": "200", "etag": '"observed"'}), b'{"id":"file-a","name":"approved.pdf","parents":["parent-b"]}'
        http = LocalHttp()
        service = build("drive", "v3", http=http, static_discovery=True, cache_discovery=False)
        t = GoogleDriveTransport(service, apply=True, allowed_ids={"file-a", "parent-a", "parent-b"})
        self.assertEqual(t.get_file("file-a")["_http_etag"], '"observed"')
        t.update_metadata({"fileId": "file-a", "name": "approved.pdf",
            "addParents": "parent-b", "removeParents": "parent-a"}, etag='"observed"')
        self.assertEqual(len(http.calls), 2)
        update = http.calls[1]
        self.assertEqual(update["method"], "PATCH")
        self.assertEqual(json.loads(update["body"]), {"name": "approved.pdf"})
        query = parse_qs(urlparse(update["uri"]).query)
        self.assertEqual(query["addParents"], ["parent-b"])
        self.assertEqual(query["removeParents"], ["parent-a"])
        self.assertEqual(update["headers"]["If-Match"], '"observed"')

    def test_combined_update_is_one_metadata_patch_with_observed_etag(self):
        t, service = transport({"id": "file-a"})
        t.update_metadata({"fileId": "file-a", "name": "approved.pdf",
            "addParents": "parent-b", "removeParents": "parent-a"}, etag='"observed-tag"')
        self.assertEqual(service.requests, [("update", {
            "fileId": "file-a", "fields": FILE_FIELDS, "supportsAllDrives": True,
            "body": {"name": "approved.pdf"}, "addParents": "parent-b", "removeParents": "parent-a"})])
        self.assertEqual(service.executions, [{"retries": 0, "headers": {"If-Match": '"observed-tag"'}}])

    def test_single_effect_updates_do_not_invent_other_effect(self):
        t, service = transport({"id": "file-a"}, {"id": "file-a"})
        t.update_metadata({"fileId": "file-a", "name": "approved.pdf"})
        t.update_metadata({"fileId": "file-a", "addParents": "parent-b", "removeParents": "parent-a"})
        self.assertEqual(service.requests[0][1]["body"], {"name": "approved.pdf"})
        self.assertNotIn("addParents", service.requests[0][1])
        self.assertEqual(service.requests[1][1]["body"], {})
        self.assertEqual(service.executions, [{"retries": 0, "headers": {}}] * 2)

    def test_get_preserves_http_etag_without_inventing_one_from_version(self):
        t, service = transport({"id": "file-a", "version": "7"}, headers={"ETag": '"tag-v7"'})
        self.assertEqual(t.get_file("file-a")["_http_etag"], '"tag-v7"')
        service.response_headers.clear()
        service.values.append({"id": "file-a", "version": "8"})
        self.assertNotIn("_http_etag", t.get_file("file-a"))
        self.assertEqual([call["retries"] for call in service.executions], [0, 0])

    def test_structured_rate_limit_403_is_retryable_but_permission_denial_is_not(self):
        try:
            import httplib2
            from googleapiclient.errors import HttpError
        except ImportError:
            self.skipTest("Optional direct dependencies are not installed")
        for reasons, transient in (
            (["rateLimitExceeded"], True),
            (["userRateLimitExceeded"], True),
            (["rateLimitExceeded", "userRateLimitExceeded"], True),
            (["insufficientFilePermissions"], False),
            (["forbidden"], False),
            (["rateLimitExceeded", "insufficientFilePermissions"], False),
            (["dailyLimitExceeded"], False),
        ):
            for write in (False, True):
                with self.subTest(reasons=reasons, write=write):
                    body = {"error": {"code": 403, "message": "provider secret",
                        "errors": [{"reason": reason, "message": "provider secret"} for reason in reasons]}}
                    error = HttpError(httplib2.Response({"status": "403"}), json.dumps(body).encode(), uri="https://secret.invalid")
                    attempts = 3 if transient and not write else 1
                    t, service = transport(*([error] * attempts), secret_values=lambda: ("provider secret",))
                    with self.assertRaises(TransportError) as caught:
                        if write:
                            t.update_metadata({"fileId": "file-a", "name": "approved.pdf"})
                        else:
                            t.get_file("file-a")
                    actual = caught.exception
                    self.assertEqual(actual.status, 403)
                    self.assertEqual(actual.transient, transient)
                    self.assertEqual(actual.code, "DRIVE_RATE_LIMIT" if transient else "DRIVE_HTTP_ERROR")
                    self.assertEqual(actual.outcome, "UNKNOWN" if write else "READ_FAILED")
                    self.assertNotIn("secret", str(actual))
                    self.assertNotIn("secret", json.dumps(actual.as_dict()))
                    self.assertEqual(len(service.executions), attempts)
                    self.assertTrue(all(x["retries"] == 0 for x in service.executions))

    def test_rate_limit_classification_does_not_guess_from_text_or_malformed_body(self):
        bodies = (b"rateLimitExceeded", b"not-json", b"[]", b'{"error": []}',
            b'{"error":{"errors":[]}}', b'{"error":{"errors":[{}]}}',
            b'{"error":{"errors":[{"reason":[]}]}}',
            b'{"error":{"message":"userRateLimitExceeded"}}', b"x" * 65537)
        for body in bodies:
            error = RuntimeError("rateLimitExceeded")
            error.resp = types.SimpleNamespace(status=403)
            error.content = body
            t, _ = transport(error)
            with self.subTest(body=body[:60]), self.assertRaises(TransportError) as caught:
                t.get_file("file-a")
            self.assertFalse(caught.exception.transient)
            self.assertEqual(caught.exception.code, "DRIVE_HTTP_ERROR")

    def test_conflict_and_transient_errors_are_classified_without_replay(self):
        for status, conflict, transient in ((412, True, False), (429, False, True),
                (503, False, True), (403, False, False), (400, False, False)):
            with self.subTest(status=status):
                error = RuntimeError("provider secret must not escape")
                error.resp = types.SimpleNamespace(status=status)
                t, service = transport(error)
                with self.assertRaises(TransportError) as caught:
                    t.update_metadata({"fileId": "file-a", "name": "approved.pdf"}, etag='"stale"')
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.conflict, conflict)
                self.assertEqual(caught.exception.transient, transient)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(service.executions), 1)
                self.assertEqual(service.executions[0]["retries"], 0)
        t, _ = transport(TimeoutError("response lost"))
        with self.assertRaises(TransportError) as caught:
            t.update_metadata({"fileId": "file-a", "name": "approved.pdf"})
        self.assertTrue(caught.exception.transient)
        self.assertEqual(caught.exception.outcome, "UNKNOWN")

    def test_combined_updates_retain_allowlist_and_metadata_guards(self):
        invalid = (
            {"fileId": "excluded", "name": "approved.pdf", "addParents": "parent-b"},
            {"fileId": "file-a", "name": "approved.pdf", "addParents": "outside"},
            {"fileId": "file-a", "name": "approved.pdf", "addParents": "parent-a", "removeParents": "parent-a"},
            {"fileId": "file-a", "name": "approved.pdf", "permissions": []},
            {"fileId": "file-a", "name": "approved.pdf", "file_uri": "body"},
            {"fileId": "file-a"},
        )
        for args in invalid:
            t, service = transport()
            with self.subTest(args=args), self.assertRaises(TransportError):
                t.update_metadata(args)
            self.assertEqual(service.requests, [])
        t, service = transport(apply=False)
        with self.assertRaisesRegex(TransportError, "READ_ONLY_TRANSPORT"):
            t.update_metadata({"fileId": "file-a", "name": "approved.pdf", "addParents": "parent-b"})
        self.assertEqual(service.requests, [])

    def test_conditional_header_cannot_be_wildcard_or_injected(self):
        for etag in ("*", "", "unquoted", '"x"\r\nAuthorization: secret', 7):
            t, service = transport()
            with self.subTest(etag=etag), self.assertRaisesRegex(TransportError, "EXACT_HTTP_ETAG_REQUIRED"):
                t.update_metadata({"fileId": "file-a", "name": "approved.pdf"}, etag=etag)
            self.assertEqual(service.requests, [])


if __name__ == "__main__":
    unittest.main()
