import copy
from pathlib import Path
import sys
import tempfile
import traceback
import types
import unittest
from unittest.mock import patch

from uqmc_recovery.direct_transport import (
    GoogleDriveTransport, TransportError, METADATA_SCOPE, READONLY_SCOPE, FULL_DRIVE_SCOPE, FILE_FIELDS,
    _single_send_http_class, _ReplayBlocked,
)


class Request:
    def __init__(self, value, calls):
        self.value, self.calls = value, calls

    def execute(self, *, num_retries):
        self.calls.append(("execute", num_retries))
        if isinstance(self.value, Exception):
            raise self.value
        return copy.deepcopy(self.value)


class Resource:
    def __init__(self, queue, calls, name):
        self.queue, self.calls, self.name = queue, calls, name

    def __getattr__(self, method):
        def build(**kwargs):
            self.calls.append((self.name + "." + method, kwargs))
            return Request(self.queue.pop(0), self.calls)
        return build


class Service:
    def __init__(self, *responses):
        self.queue, self.calls = list(responses), []

    def files(self):
        return Resource(self.queue, self.calls, "files")

    def permissions(self):
        return Resource(self.queue, self.calls, "permissions")


class DirectTransportTests(unittest.TestCase):
    def transport(self, *responses, apply=False, refresh=None):
        service = Service(*responses)
        return GoogleDriveTransport(service, apply=apply,
            allowed_ids={"file-a", "parent-a", "parent-b", "withdrawn"},
            excluded_ids={"withdrawn"}, refresh_credentials=refresh), service

    def test_metadata_only_rename_and_move_use_exact_args_once(self):
        transport, service = self.transport({"id": "file-a"}, {"id": "file-a"}, apply=True)
        transport.update_file({"fileId": "file-a", "name": "Exact title.pdf"})
        transport.update_file({"fileId": "file-a", "addParents": "parent-b", "removeParents": "parent-a"})
        self.assertEqual(service.calls, [
            ("files.update", {"fileId": "file-a", "body": {"name": "Exact title.pdf"},
                              "fields": FILE_FIELDS, "supportsAllDrives": True}),
            ("execute", 0),
            ("files.update", {"fileId": "file-a", "body": {}, "addParents": "parent-b",
                              "removeParents": "parent-a", "fields": FILE_FIELDS,
                              "supportsAllDrives": True}),
            ("execute", 0),
        ])

    def test_readonly_and_invalid_arguments_never_build_a_write(self):
        transport, service = self.transport({"id": "file-a"})
        with self.assertRaisesRegex(TransportError, "READ_ONLY_TRANSPORT"):
            transport.update_file({"fileId": "file-a", "name": "new"})
        self.assertEqual(service.calls, [])
        transport.apply_enabled = True
        for args in (
            {"fileId": "file-a", "file_uri": "upload"},
            {"fileId": "file-a", "name": "new", "mime_type": "type"},
            {"fileId": "file-a", "name": "new", "addParents": "parent-b"},
            {"fileId": "file-a"},
            {"fileId": "file-a", "name": ""},
            {"fileId": "file-a", "addParents": "parent-a,parent-a"},
            {"fileId": "file-a", "addParents": "parent-a", "removeParents": "parent-a"},
            {"fileId": "file-a", "addParents": "file-a"},
        ):
            with self.subTest(args=args), self.assertRaises(TransportError):
                transport.update_file(args)
        self.assertEqual(service.calls, [])

    def test_all_exact_id_guards_apply_before_requests(self):
        transport, service = self.transport(apply=True)
        for bad_id in ("withdrawn", "unapproved", "root", "x' or trashed=true", None):
            for name in ("get_file", "list_children", "list_permissions"):
                with self.subTest(method=name, value=bad_id), self.assertRaises(TransportError):
                    getattr(transport, name)(bad_id)
            with self.assertRaises(TransportError):
                transport.update_file({"fileId": bad_id, "name": "new"})
            with self.assertRaises(TransportError):
                transport.update_file({"fileId": "file-a", "addParents": bad_id})
        self.assertEqual(service.calls, [])

    def test_reads_and_complete_pagination(self):
        transport, service = self.transport(
            {"id": "file-a", "name": "exact"},
            {"files": [{"id": "outside-listed-child"}], "nextPageToken": "page2"},
            {"files": [{"id": "file-a"}]},
            {"permissions": [{"id": "123"}], "nextPageToken": "permission2"},
            {"permissions": [{"id": "anyoneWithLink"}]},
        )
        self.assertEqual(transport.get_file("file-a")["name"], "exact")
        self.assertEqual([x["id"] for x in transport.list_children("parent-a")],
                         ["outside-listed-child", "file-a"])
        self.assertEqual([x["id"] for x in transport.list_permissions("file-a")],
                         ["123", "anyoneWithLink"])
        self.assertTrue(all(call == ("execute", 0) for call in service.calls if call[0] == "execute"))
        lists = [call[1] for call in service.calls if call[0] == "files.list"]
        self.assertEqual([q["pageToken"] for q in lists], [None, "page2"])
        self.assertEqual(lists[0]["q"], "'parent-a' in parents and trashed = false")
        self.assertEqual(len([call for call in service.calls if call[0] == "files.get"]), 1)

    def test_incomplete_and_ambiguous_lists_fail_closed(self):
        for responses in (
            [{"files": [], "incompleteSearch": True}],
            [{}],
            [{"files": [{"id": "file-a"}], "nextPageToken": "p"},
             {"files": [{"id": "file-a"}]}],
            [{"files": [], "nextPageToken": "p"}, {"files": [], "nextPageToken": "p"}],
            [{"files": [{"id": "bad/id"}]}],
        ):
            with self.subTest(responses=responses):
                transport, _ = self.transport(*responses)
                with self.assertRaises(TransportError):
                    transport.list_children("parent-a")

    def test_errors_are_redacted_and_never_retried_after_write(self):
        class ProviderError(Exception):
            resp = types.SimpleNamespace(status=503)
        error = ProviderError("Authorization: Bearer secret-token; refresh_token=secret-refresh")
        transport, service = self.transport(error, apply=True)
        try:
            transport.update_file({"fileId": "file-a", "name": "new"})
        except TransportError as captured:
            output = "".join(traceback.format_exception(captured))
            self.assertNotIn("secret-token", output)
            self.assertNotIn("secret-refresh", output)
            self.assertEqual(captured.as_dict(),
                             {"code": "DRIVE_HTTP_ERROR", "status": 503, "outcome": "UNKNOWN", "request": "files.update", "item_id": "file-a"})
        else:
            self.fail("Expected sanitized error")
        self.assertEqual(sum(call[0] == "files.update" for call in service.calls), 1)
        self.assertEqual([x for x in service.calls if x[0] == "execute"], [("execute", 0)])

    def test_refresh_happens_before_request_and_auth_failure_does_not_dispatch(self):
        order = []
        transport, service = self.transport({"id": "file-a"}, apply=True,
                                           refresh=lambda: order.append("refresh"))
        transport.update_file({"fileId": "file-a", "name": "new"})
        self.assertEqual(order, ["refresh"])
        def fail():
            raise RuntimeError("secret-auth-response")
        transport, service = self.transport(apply=True, refresh=fail)
        with self.assertRaisesRegex(TransportError, "^AUTH_REFRESH_FAILED$") as error:
            transport.update_file({"fileId": "file-a", "name": "new"})
        self.assertEqual(error.exception.outcome, "NOT_DISPATCHED")
        self.assertEqual(service.calls, [])

    def test_wrong_response_identity_is_never_success(self):
        transport, _ = self.transport({"id": "other"}, {"id": "other"}, apply=True)
        with self.assertRaisesRegex(TransportError, "DRIVE_RESPONSE_ID_MISMATCH"):
            transport.get_file("file-a")
        with self.assertRaisesRegex(TransportError, "DRIVE_WRITE_RESPONSE_UNPROVEN") as error:
            transport.update_file({"fileId": "file-a", "name": "new"})
        self.assertEqual(error.exception.outcome, "UNKNOWN")

    def test_httplib_socket_replay_is_blocked_before_second_send(self):
        class Connection:
            def __init__(self):
                self.actual_sends = 0
            def request(self, *args):
                self.actual_sends += 1
        class RetryingHttp:
            def request(self, connection):
                return self._conn_request(connection)
            def _conn_request(self, connection):
                connection.request("PATCH", "/fixture")
                # Simulate httplib2's BadStatusLine retry irrespective of RETRIES.
                connection.request("PATCH", "/fixture")
        http = _single_send_http_class(RetryingHttp)()
        connection = Connection()
        with self.assertRaises(_ReplayBlocked):
            http.request(connection)
        self.assertEqual(connection.actual_sends, 1)

    def test_full_drive_transport_retains_exact_id_and_apply_guards(self):
        try:
            from google.oauth2.credentials import Credentials
        except ImportError:
            self.skipTest("Optional direct dependencies are not installed")
        credentials = Credentials(token="fixture-token", scopes=[FULL_DRIVE_SCOPE])
        service = Service({"id": "file-a"})
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "token-drive.json"
            token.write_text("synthetic fixture only")
            token.chmod(0o600)
            with patch.object(Credentials, "from_authorized_user_file", return_value=credentials), patch("googleapiclient.discovery.build", return_value=service):
                transport = GoogleDriveTransport.from_authorized_user_file(token, apply=True,
                    allowed_ids={"file-a", "parent-a"}, mutation_ids={"file-a"})
                self.assertEqual(service.calls, [])
                for identity, error in (("unapproved", "OUTSIDE_FROZEN_SCOPE"), ("parent-a", "OUTSIDE_APPROVED_MUTATION_IDS")):
                    with self.assertRaisesRegex(TransportError, error):
                        transport.update_metadata({"fileId": identity, "name": "forbidden"})
                self.assertEqual(service.calls, [])
                transport.update_metadata({"fileId": "file-a", "name": "approved.pdf"})
                self.assertEqual(len([x for x in service.calls if x[0] == "files.update"]), 1)
                preview = GoogleDriveTransport.from_authorized_user_file(token, allowed_ids={"file-a"})
                with self.assertRaisesRegex(TransportError, "READ_ONLY_TRANSPORT"):
                    preview.update_metadata({"fileId": "file-a", "name": "blocked"})
                self.assertEqual(token.read_text(), "synthetic fixture only")

    def test_full_drive_refresh_revalidates_actual_grant_before_dispatch(self):
        try:
            from google.oauth2.credentials import Credentials
        except ImportError:
            self.skipTest("Optional direct dependencies are not installed")
        from datetime import datetime, timedelta, timezone
        for grants, allowed in (([FULL_DRIVE_SCOPE], True), ([READONLY_SCOPE], False),
                                ([FULL_DRIVE_SCOPE, "https://www.googleapis.com/auth/spreadsheets"], False)):
            with self.subTest(grants=grants), tempfile.TemporaryDirectory() as folder:
                credentials = Credentials(token="expired-fixture", refresh_token="fixture-refresh", scopes=[FULL_DRIVE_SCOPE],
                    expiry=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1))
                def refresh(request):
                    credentials._granted_scopes = grants
                    credentials.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
                token = Path(folder) / "token-drive.json"
                token.write_text("fixture only")
                token.chmod(0o600)
                service = Service({"id": "file-a"})
                with patch.object(Credentials, "from_authorized_user_file", return_value=credentials), patch.object(credentials, "refresh", side_effect=refresh) as refresh_call, patch("googleapiclient.discovery.build", return_value=service):
                    transport = GoogleDriveTransport.from_authorized_user_file(token, apply=True, allowed_ids={"file-a"})
                    if allowed:
                        transport.update_metadata({"fileId": "file-a", "name": "approved.pdf"})
                    else:
                        with self.assertRaisesRegex(TransportError, "EXACT_SUPPORTED_DRIVE_SCOPE_REQUIRED"):
                            transport.update_metadata({"fileId": "file-a", "name": "approved.pdf"})
                        self.assertEqual(service.calls, [])
                    refresh_call.assert_called_once()
                    self.assertEqual(token.read_text(), "fixture only")

    def test_transport_rejects_mixed_or_unrelated_scope_profiles(self):
        try:
            from google.oauth2.credentials import Credentials
        except ImportError:
            self.skipTest("Optional direct dependencies are not installed")
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "fixture.json"
            token.write_text("fixture only")
            token.chmod(0o600)
            for scopes in ([FULL_DRIVE_SCOPE, METADATA_SCOPE], [READONLY_SCOPE, METADATA_SCOPE],
                           [FULL_DRIVE_SCOPE, "https://www.googleapis.com/auth/spreadsheets"], []):
                with self.subTest(scopes=scopes):
                    credentials = Credentials(token="fixture", scopes=scopes)
                    with patch.object(Credentials, "from_authorized_user_file", return_value=credentials), patch("googleapiclient.discovery.build") as build:
                        with self.assertRaisesRegex(TransportError, "EXACT_SUPPORTED_DRIVE_SCOPE_REQUIRED"):
                            GoogleDriveTransport.from_authorized_user_file(token, allowed_ids={"file-a"})
                        build.assert_not_called()

    def test_explicit_token_loader_uses_supported_auth_without_grant_or_persistence(self):
        calls = []
        credentials = types.SimpleNamespace(scopes=[METADATA_SCOPE], granted_scopes=None,
                                             valid=False, refresh_token="private-fixture-token")
        def refresh(request):
            calls.append(("refresh", request))
            credentials.valid = True
        credentials.refresh = refresh
        def from_file(path):
            calls.append(("load_explicit", path))
            return credentials
        def authorized(creds, **kwargs):
            calls.append(("authorized", kwargs))
            return "authorized-http"
        service = Service({"id": "file-a"})
        def build(*args, **kwargs):
            calls.append(("build", args, kwargs))
            return service
        fake_http = types.ModuleType("httplib2")
        fake_http.RETRIES = 2
        fake_http.debuglevel = 1
        class FakeHttp:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
        fake_http.Http = FakeHttp
        fake_auth_http = types.ModuleType("google_auth_httplib2")
        fake_auth_http.AuthorizedHttp = authorized
        fake_auth_http.Request = lambda http: ("refresh-request", http)
        fake_credentials = types.ModuleType("google.oauth2.credentials")
        fake_credentials.Credentials = types.SimpleNamespace(from_authorized_user_file=from_file)
        fake_discovery = types.ModuleType("googleapiclient.discovery")
        fake_discovery.build = build
        modules = {"httplib2": fake_http, "google_auth_httplib2": fake_auth_http,
                   "google": types.ModuleType("google"), "google.oauth2": types.ModuleType("google.oauth2"),
                   "google.oauth2.credentials": fake_credentials,
                   "googleapiclient": types.ModuleType("googleapiclient"),
                   "googleapiclient.discovery": fake_discovery}
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, modules):
            token = Path(folder)/"fixture-token.json"
            token.write_text("fixture-only")
            token.chmod(0o600)
            transport = GoogleDriveTransport.from_authorized_user_file(
                token, apply=True, allowed_ids={"file-a"})
            self.assertFalse(any(call[0] == "refresh" for call in calls))
            transport.update_file({"fileId": "file-a", "name": "new"})
            self.assertEqual(token.read_text(), "fixture-only")
            self.assertEqual(fake_http.RETRIES, 1)
            self.assertEqual(fake_http.debuglevel, 0)
            auth = next(call[1] for call in calls if call[0] == "authorized")
            self.assertEqual(auth["max_refresh_attempts"], 0)
            self.assertEqual(auth["refresh_status_codes"], ())
            self.assertFalse(auth["http"].follow_redirects)
            self.assertEqual(auth["http"].timeout, 15)
            self.assertEqual([call[0] for call in calls].count("refresh"), 1)
            refresh_request = next(call[1] for call in calls if call[0] == "refresh")
            self.assertIs(refresh_request[1], auth["http"])
            self.assertEqual(refresh_request[1].timeout, 15)
            credentials.scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            with self.assertRaisesRegex(TransportError, "EXACT_SUPPORTED_DRIVE_SCOPE_REQUIRED"):
                GoogleDriveTransport.from_authorized_user_file(token, allowed_ids={"file-a"})
            credentials.scopes = [READONLY_SCOPE]
            with self.assertRaisesRegex(TransportError, "EXACT_SUPPORTED_DRIVE_SCOPE_REQUIRED"):
                GoogleDriveTransport.from_authorized_user_file(token, apply=True, allowed_ids={"file-a"})
            preview = GoogleDriveTransport.from_authorized_user_file(token, allowed_ids={"file-a"})
            self.assertFalse(preview.apply_enabled)
            token.chmod(0o644)
            with self.assertRaisesRegex(TransportError, "TOKEN_FILE_REQUIRES_PRIVATE_PERMISSIONS"):
                GoogleDriveTransport.from_authorized_user_file(token, allowed_ids={"file-a"})


if __name__ == "__main__":
    unittest.main()
