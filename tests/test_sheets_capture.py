"""Offline fake Sheets/Drive capture tests; no live files or credentials."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from uqmc_recovery.direct_transport import FULL_DRIVE_SCOPE, METADATA_SCOPE, TransportError
from uqmc_recovery.importer import HEADERS, workbook_fingerprint
from uqmc_recovery.model import SafetyError
from uqmc_recovery.sheets_capture import GoogleSheetsCapture, normalize_capture, FIELDS


def literal(value):
    return {"userEnteredValue": {"stringValue": value}, "effectiveValue": {"stringValue": value}, "formattedValue": value}


def response():
    return {"spreadsheetId": "fixture-workbook", "sheets": [
        {"properties": {"sheetId": index, "title": title,
            "gridProperties": {"rowCount": 100, "columnCount": 26}},
         "data": [{"rowData": [{"values": [literal(x) for x in headers]}]}]}
        for index, (title, headers) in enumerate(HEADERS.items())]}


class Request:
    def __init__(self, value):
        self.value = value
    def execute(self, num_retries=0):
        if isinstance(self.value, Exception):
            raise self.value
        return copy.deepcopy(self.value)


class Service:
    def __init__(self, values):
        self.values, self.calls = values, []
    def files(self):
        return self
    def spreadsheets(self):
        return self
    def get(self, **kwargs):
        self.calls.append(kwargs)
        return Request(self.values.pop(0))


class FakeDrive:
    def __init__(self, revisions):
        self.service = Service(revisions)
        self.requests = []
    def _guard(self, value):
        if value != "fixture-workbook":
            raise SafetyError("OUTSIDE_FROZEN_SCOPE")
    def _read(self, factory, **context):
        self.requests.append(context)
        return factory().execute(num_retries=0)


class SheetsCaptureTests(unittest.TestCase):
    def setUp(self):
        self.revision = {"id": "fixture-workbook", "mimeType": "application/vnd.google-apps.spreadsheet",
            "version": "12", "modifiedTime": "2026-10-21T00:00:00Z", "trashed": False}
    def capture(self, data=None, after=None):
        self.drive = FakeDrive([self.revision, after or self.revision])
        self.sheets = Service([response() if data is None else data])
        return GoogleSheetsCapture(self.drive, self.sheets, workbook_id="fixture-workbook").capture()
    def test_single_data_read_preserves_formula_and_effective_value(self):
        data = response()
        formula = {"userEnteredValue": {"formulaValue": '="Pending"'},
            "effectiveValue": {"stringValue": "Pending"}, "formattedValue": "Pending"}
        data["sheets"][0]["data"][0]["rowData"].append({"values": [formula]})
        result = self.capture(data)
        self.assertEqual(result["sheets"]["Proposed Moves"][1], ["Pending"])
        self.assertEqual(result["cell_evidence"]["Proposed Moves"]["rowData"][1]["values"][0], formula)
        self.assertEqual(len(self.sheets.calls), 1)
        self.assertEqual(len(self.drive.service.calls), 2)
        self.assertEqual(self.sheets.calls[0]["fields"], FIELDS)
        self.assertTrue(all("!A:" in item for item in self.sheets.calls[0]["ranges"]))
        self.assertEqual(result["source_revision"]["version"], "12")
        self.assertEqual(result["input_sha256"], workbook_fingerprint(result))
    def test_formula_hash_changes_even_when_effective_value_unchanged(self):
        first, second = response(), response()
        for data, formula in ((first, '="Value"'), (second, '=CONCAT("Val","ue")')):
            data["sheets"][0]["data"][0]["rowData"].append({"values": [{
                "userEnteredValue": {"formulaValue": formula}, "effectiveValue": {"stringValue": "Value"}}]})
        a, b = self.capture(first), self.capture(second)
        self.assertEqual(a["input_sha256"], b["input_sha256"])
        self.assertNotEqual(a["capture_sha256"], b["capture_sha256"])
    def test_changed_revision_rejects_capture(self):
        with self.assertRaisesRegex(SafetyError, "WORKBOOK_CHANGED_DURING_CAPTURE"):
            self.capture(after=self.revision | {"version": "13"})
    def test_missing_tab_truncated_start_or_missing_grid_fails_closed(self):
        for kind in ("missing", "start", "grid", "wrong_id"):
            with self.subTest(kind=kind):
                data = response()
                if kind == "missing": data["sheets"].pop()
                if kind == "start": data["sheets"][0]["data"][0]["startRow"] = 1
                if kind == "grid": data["sheets"][0]["properties"].pop("gridProperties")
                if kind == "wrong_id": data["spreadsheetId"] = "wrong-workbook"
                with self.assertRaises(SafetyError): self.capture(data)
    def test_formula_error_or_missing_result_fails_closed(self):
        for effective in ({}, {"errorValue": {"type": "REF", "message": "No reference"}}):
            data = response()
            data["sheets"][0]["data"][0]["rowData"].append({"values": [{
                "userEnteredValue": {"formulaValue": "=A100"}, "effectiveValue": effective}]})
            with self.assertRaises(SafetyError): self.capture(data)
    def test_unavailable_read_is_not_retried_or_replaced_by_cache(self):
        with self.assertRaisesRegex(TransportError, "DRIVE_REQUEST_TIMEOUT"):
            self.capture(TransportError("DRIVE_REQUEST_TIMEOUT"))
        self.assertEqual(len(self.sheets.calls), 1)
        self.assertEqual(len(self.drive.service.calls), 1)
    def test_missing_version_rejected_before_sheet_read(self):
        self.revision.pop("version")
        with self.assertRaisesRegex(SafetyError, "WORKBOOK_REVISION_UNAVAILABLE"):
            self.capture()
        self.assertEqual(self.sheets.calls, [])
    def test_factory_reuses_full_scope_http_and_fifteen_second_timeout(self):
        http = SimpleNamespace(credentials=SimpleNamespace(granted_scopes=None, scopes=[FULL_DRIVE_SCOPE]))
        drive = SimpleNamespace(service=SimpleNamespace(_http=http), _guard=lambda value: None)
        with patch("uqmc_recovery.sheets_capture.GoogleDriveTransport.from_authorized_user_file", return_value=drive) as factory, \
                patch("googleapiclient.discovery.build", return_value=object()) as build:
            GoogleSheetsCapture.from_authorized_user_file("fixture-token.json", workbook_id="fixture-workbook")
            self.assertEqual(factory.call_args.kwargs["timeout"], 15)
            self.assertFalse(factory.call_args.kwargs["apply"])
            self.assertEqual(factory.call_args.kwargs["mutation_ids"], set())
            self.assertIs(build.call_args.kwargs["http"], http)
            http.credentials.scopes = [METADATA_SCOPE]
            with self.assertRaisesRegex(TransportError, "FULL_DRIVE_SCOPE"):
                GoogleSheetsCapture.from_authorized_user_file("fixture-token.json", workbook_id="fixture-workbook")


if __name__ == "__main__":
    unittest.main()
