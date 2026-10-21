"""One read-only, revision-checked capture of the canonical proposal ranges.

The existing full-Drive grant is accepted by Sheets spreadsheets.get. This does
not initiate OAuth, save tokens, request another scope, or expose a write method.
Enable Google Sheets API in the existing client project before using it live.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from .direct_transport import GoogleDriveTransport, TransportError, FULL_DRIVE_SCOPE
from .importer import HEADERS, workbook_fingerprint
from .model import SafetyError, digest

CELL_FIELDS = "userEnteredValue,effectiveValue,formattedValue"
FIELDS = ("spreadsheetId,properties(title),sheets(properties(sheetId,title,"
          "gridProperties(rowCount,columnCount)),data(startRow,startColumn,"
          "rowData(values(" + CELL_FIELDS + "))))")
REVISION_FIELDS = "id,mimeType,version,modifiedTime,trashed"


def _utcnow():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _column(number):
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def _literal(value):
    if not isinstance(value, dict):
        raise SafetyError("WORKBOOK_CELL_VALUE_INVALID")
    if not value:
        return ""
    if "errorValue" in value:
        raise SafetyError("WORKBOOK_CELL_FORMULA_ERROR")
    if len(value) != 1:
        raise SafetyError("WORKBOOK_CELL_VALUE_AMBIGUOUS")
    key, result = next(iter(value.items()))
    if ((key == "stringValue" and isinstance(result, str)) or
            (key == "numberValue" and isinstance(result, (int, float)) and not isinstance(result, bool)) or
            (key == "boolValue" and isinstance(result, bool))):
        return result
    raise SafetyError("WORKBOOK_CELL_VALUE_INVALID")


def _cell_value(cell, *, allow_unavailable_display=False):
    if not isinstance(cell, dict):
        raise SafetyError("WORKBOOK_CELL_INVALID")
    entered, effective = cell.get("userEnteredValue", {}), cell.get("effectiveValue", {})
    if not isinstance(entered, dict) or not isinstance(effective, dict):
        raise SafetyError("WORKBOOK_CELL_VALUE_INVALID")
    if "formulaValue" in entered:
        if len(entered) != 1 or not isinstance(entered["formulaValue"], str):
            raise SafetyError("WORKBOOK_FORMULA_RESULT_UNAVAILABLE")
        if not effective:
            if allow_unavailable_display:
                return None  # Unknown display result, never an invented empty string.
            raise SafetyError("WORKBOOK_FORMULA_RESULT_UNAVAILABLE")
        return _literal(effective)
    return _literal(effective if effective else entered)


def normalize_capture(response, *, workbook_id, revision, started_at_utc, captured_at_utc):
    """Preserve cell formulas and values alongside importer-compatible matrices."""
    if not isinstance(response, dict) or response.get("spreadsheetId") != workbook_id:
        raise SafetyError("WRONG_WORKBOOK_CAPTURE")
    sheets = response.get("sheets")
    if not isinstance(sheets, list):
        raise SafetyError("WORKBOOK_CAPTURE_INCOMPLETE")
    indexed = {}
    for sheet in sheets:
        if not isinstance(sheet, dict) or not isinstance(sheet.get("properties"), dict):
            raise SafetyError("WORKBOOK_SHEET_METADATA_INVALID")
        title = sheet["properties"].get("title")
        if title in HEADERS:
            if title in indexed:
                raise SafetyError("DUPLICATE_WORKBOOK_SHEET")
            indexed[title] = sheet
    if set(indexed) != set(HEADERS):
        raise SafetyError("WORKBOOK_CAPTURE_INCOMPLETE")
    matrices, evidence, unavailable = {}, {}, []
    for title, required in HEADERS.items():
        sheet = indexed[title]
        grid = sheet["properties"].get("gridProperties", {})
        row_count, column_count = grid.get("rowCount"), grid.get("columnCount")
        if (type(row_count) is not int or row_count < 1 or type(column_count) is not int or
                column_count < len(required) or type(sheet["properties"].get("sheetId")) is not int):
            raise SafetyError("WORKBOOK_GRID_METADATA_INVALID")
        blocks = sheet.get("data")
        if (not isinstance(blocks, list) or len(blocks) != 1 or
                not isinstance(blocks[0], dict) or blocks[0].get("startRow", 0) != 0 or
                blocks[0].get("startColumn", 0) != 0):
            raise SafetyError("WORKBOOK_RANGE_NOT_COMPLETE")
        rows = blocks[0].get("rowData")
        if not isinstance(rows, list) or not rows or len(rows) > row_count:
            raise SafetyError("WORKBOOK_RANGE_NOT_COMPLETE")
        matrix = []
        for row_index, row in enumerate(rows):
            cells = row.get("values", []) if isinstance(row, dict) else None
            if not isinstance(cells, list) or len(cells) > len(required):
                raise SafetyError("WORKBOOK_CAPTURE_RANGE_MISMATCH")
            values = []
            for column_index, cell in enumerate(cells):
                # Only this existing nonexecuting reference/display column may
                # have an unavailable computed result. Check its actual header
                # as well as coordinate, so column reordering cannot relax an
                # identity, approval, proposed-name or destination field.
                display = (title == "Rename Coverage" and row_index > 0 and
                    column_index == 5 and len(matrix[0]) > 5 and
                    matrix[0][5] == "Rename proposal")
                value = _cell_value(cell, allow_unavailable_display=display)
                values.append(value)
                if value is None:
                    unavailable.append({"sheet": title, "row": row_index + 1,
                        "column": column_index + 1,
                        "a1": "'Rename Coverage'!F" + str(row_index + 1),
                        "reason": "NONEXECUTING_DISPLAY_FORMULA_RESULT_UNAVAILABLE"})
            while values and values[-1] == "":
                values.pop()
            matrix.append(values)
        while len(matrix) > 1 and not matrix[-1]:
            matrix.pop()
        matrices[title] = matrix
        evidence[title] = {"properties": copy.deepcopy(sheet["properties"]),
            "requested_range": "'" + title.replace("'", "''") + "'!A:" + _column(len(required)),
            "rowData": copy.deepcopy(rows)}
    result = {"workbook_id": workbook_id, "exported_at_utc": captured_at_utc,
        "sheets": matrices, "complete": {title: True for title in HEADERS},
        "cell_evidence": evidence, "source_revision": copy.deepcopy(revision),
        "capture_started_at_utc": started_at_utc,
        "unavailable_formula_cells": unavailable,
        "unavailable_formula_cell_count": len(unavailable),
        "capture_method": "sheets.spreadsheets.get_with_drive_revision_bracket"}
    result["input_sha256"] = workbook_fingerprint(result, expected_workbook_id=workbook_id)
    result["capture_sha256"] = digest({key: result[key] for key in
        ("workbook_id", "sheets", "complete", "cell_evidence", "source_revision")})
    return result


class GoogleSheetsCapture:
    def __init__(self, drive_transport, sheets_service, *, workbook_id):
        if not GoogleDriveTransport._valid_id(workbook_id):
            raise SafetyError("EXPLICIT_WORKBOOK_ID_REQUIRED")
        self.drive, self.service, self.workbook_id = drive_transport, sheets_service, workbook_id
        self.drive._guard(workbook_id)

    def _revision(self):
        result = self.drive._read(lambda: self.drive.service.files().get(
            fileId=self.workbook_id, fields=REVISION_FIELDS, supportsAllDrives=True),
            request="files.get", item_id=self.workbook_id)
        if (result.get("id") != self.workbook_id or
                result.get("mimeType") != "application/vnd.google-apps.spreadsheet" or
                result.get("trashed") is not False or
                not isinstance(result.get("version"), str) or not result["version"].isdecimal() or
                not isinstance(result.get("modifiedTime"), str) or not result["modifiedTime"]):
            raise SafetyError("WORKBOOK_REVISION_UNAVAILABLE")
        return {key: result[key] for key in ("id", "mimeType", "version", "modifiedTime", "trashed")}

    def capture(self):
        """Exactly one Sheets data fetch; no retries or stale-cache fallback."""
        started = _utcnow()
        before = self._revision()
        ranges = ["'" + title.replace("'", "''") + "'!A:" + _column(len(headers))
                  for title, headers in HEADERS.items()]
        response = self.drive._read(lambda: self.service.spreadsheets().get(
            spreadsheetId=self.workbook_id, ranges=ranges, fields=FIELDS),
            request="spreadsheets.get", item_id=self.workbook_id)
        after = self._revision()
        if before != after:
            raise SafetyError("WORKBOOK_CHANGED_DURING_CAPTURE")
        return normalize_capture(response, workbook_id=self.workbook_id, revision=after,
            started_at_utc=started, captured_at_utc=_utcnow())

    @classmethod
    def from_authorized_user_file(cls, token_path, *, workbook_id, timeout=15):
        """Reuse one explicit existing full-Drive grant and its bounded HTTP stack."""
        drive = GoogleDriveTransport.from_authorized_user_file(token_path, apply=False,
            allowed_ids={workbook_id}, mutation_ids=set(), audit_permissions=False, timeout=timeout)
        # The Drive factory already enforces private token permissions, scope
        # validation, zero hidden retries, static discovery and redacted errors.
        http = drive.service._http
        credentials = http.credentials
        if set(credentials.granted_scopes or credentials.scopes or ()) != {FULL_DRIVE_SCOPE}:
            raise TransportError("EXISTING_FULL_DRIVE_SCOPE_REQUIRED_FOR_SHEETS_CAPTURE")
        try:
            from googleapiclient.discovery import build
            sheets = build("sheets", "v4", http=http, cache_discovery=False, static_discovery=True)
        except ImportError:
            raise TransportError("INSTALL_DIRECT_OPTIONAL_DEPENDENCIES") from None
        except Exception:
            raise TransportError("SHEETS_CLIENT_INITIALIZATION_FAILED") from None
        return cls(drive, sheets, workbook_id=workbook_id)
