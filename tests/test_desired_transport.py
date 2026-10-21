"""Combined metadata requests and conditional headers; no credentials/network."""
import copy
import json
import types
import unittest
from urllib.parse import parse_qs, urlparse

from uqmc_recovery.direct_transport import GoogleDriveTransport, TransportError, FILE_FIELDS


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

    def update(self, **kwargs):
        self.requests.append(("update", kwargs))
        return Request(self, self.values.pop(0))


def transport(*values, apply=True, headers=None):
    service = Service(*values, headers=headers)
    return GoogleDriveTransport(service, apply=apply,
        allowed_ids={"file-a", "parent-a", "parent-b", "excluded"},
        excluded_ids={"excluded"}), service


class DesiredTransportTests(unittest.TestCase):
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
        self.assertEqual([call["retries"] for call in service.executions], [2, 2])

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
