"""Offline, report-only duplicate review. Reads metadata, never Drive or file bodies.

This module is deliberately independent of the executor, journal and adapters.
Hash trust is an explicit local assertion; it is not remotely authenticated here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


NATIVE_PREFIX = "application/vnd.google-apps."
SHORTCUT = NATIVE_PREFIX + "shortcut"
FOLDER = NATIVE_PREFIX + "folder"
HEX_LENGTHS = {"md5": 32, "sha256": 64}
MIN_FINGERPRINT_TOKENS = 8
MAX_CANDIDATE_PAIRS = 250_000
MAX_PAIR_EXAMINATIONS = 2_000_000
MAX_FINDINGS = 250_000
MAX_FINGERPRINT_TOKENS = 4096
RECOMMENDATION = (
    "Manual review only. Preserve all records and their audit, working, certified "
    "and repeated-trip context. Confirm current versions, ownership, permissions, "
    "comments, revisions, formulas, links and signatures before any separately "
    "authorised decision. No preferred original or action is selected."
)
BASE_UNCERTAINTY = [
    "This offline snapshot does not establish current Drive state or complete scope.",
    "Matching bytes or content do not prove that distinct Drive objects are interchangeable.",
]
NATIVE_UNCERTAINTY = (
    "Google-native files have no comparable original binary MD5. Equal exports can "
    "omit comments, revisions, formulas, links, permissions, ownership, signatures "
    "and context, so they do not establish full equivalence."
)
FILE_FIELDS = {
    "id", "name", "mimeType", "size", "version", "md5Checksum", "sha256Checksum",
    "parents", "current_scope", "labels", "webViewLink", "shortcutDetails",
    "review_status", "in_scope", "modifiedTime", "provenance",
}


class DedupError(ValueError):
    """A fail-closed input or report error; messages never include raw records."""


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _schema(value, list_key):
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or not isinstance(value.get(list_key), list)):
        raise DedupError("INVALID_SCHEMA_OR_RECORD_LIST")


def _provenance(value):
    if not isinstance(value, dict) or any(not _text(value.get(k)) for k in ("source", "reference", "captured_at")):
        raise DedupError("SOURCE_PROVENANCE_REQUIRED")
    try:
        parsed = datetime.fromisoformat(value["captured_at"].replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        raise DedupError("INVALID_PROVENANCE_CAPTURE_TIME") from None
    return {k: value[k] for k in ("source", "reference", "captured_at")}


def _policy(config):
    keys = ("excluded_ids", "withdrawn_ids", "denied_source_ids")
    if not isinstance(config, dict) or not any(k in config for k in keys):
        raise DedupError("EXPLICIT_PRIVATE_IGNORE_CONFIG_REQUIRED")
    result = set()
    for key in (*keys, "trusted_checksum_sources"):
        values = config.get(key, [])
        if not isinstance(values, list) or any(not _text(v) for v in values):
            raise DedupError("INVALID_IGNORE_CONFIG_LIST")
        if key in keys:
            result.update(values)
    return result, set(config.get("trusted_checksum_sources", []))


def _size(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        try:
            return int(value)
        except ValueError:
            pass
    return None


def _digest(value, algorithm):
    return (isinstance(value, str) and isinstance(algorithm, str) and algorithm in HEX_LENGTHS
            and re.fullmatch(r"[0-9a-fA-F]{%d}" % HEX_LENGTHS[algorithm], value) is not None)


def _name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _copy_name(value):
    # Only a candidate key. Dates, version suffixes and other labels are preserved.
    return re.sub(r"^(?:copy of\s+)+", "", _name(value))


def _link_target(value):
    """Recognise an ID in a supplied canonical link; never request the URL."""
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if url.scheme != "https" or url.hostname not in {"drive.google.com", "docs.google.com"}:
        return None
    match = re.search(r"/(?:d|folders)/([A-Za-z0-9_-]+)(?:/|$)", url.path)
    if match:
        return match.group(1)
    ids = parse_qs(url.query).get("id", [])
    return ids[0] if len(ids) == 1 else None


def _issue(issues, file_id, code, message):
    item = {"file_id": file_id, "code": code, "message": message}
    if item not in issues:
        issues.append(item)


def _record(raw, source, scope):
    if set(raw) - FILE_FIELDS:
        raise DedupError("UNSUPPORTED_FILE_FIELDS_METADATA_ONLY")
    if any(not _text(raw.get(k)) for k in ("id", "name", "mimeType")):
        raise DedupError("INVALID_FILE_METADATA")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", raw["id"]) or raw["id"] == "root":
        raise DedupError("STABLE_FILE_ID_REQUIRED_NOT_A_PATH_URL_OR_ROOT_ALIAS")
    mime = raw["mimeType"].strip().casefold()
    for key in ("parents", "labels"):
        if not isinstance(raw.get(key, []), list) or any(not _text(v) for v in raw.get(key, [])):
            raise DedupError("INVALID_FILE_METADATA_LIST")
    for key in ("version", "webViewLink", "modifiedTime", "current_scope", "review_status"):
        if key in raw and not _text(raw[key]):
            raise DedupError("INVALID_FILE_METADATA_TEXT")
    if "in_scope" in raw and type(raw["in_scope"]) is not bool:
        raise DedupError("INVALID_SCOPE_FLAG")
    if "shortcutDetails" in raw:
        details = raw["shortcutDetails"]
        if not isinstance(details, dict) or not _text(details.get("targetId")):
            raise DedupError("INVALID_SHORTCUT_TARGET")
        if mime != SHORTCUT:
            raise DedupError("SHORTCUT_MIME_REQUIRED")
    elif mime == SHORTCUT:
        raise DedupError("SHORTCUT_TARGET_REQUIRED")
    provenance = _provenance(raw["provenance"]) if "provenance" in raw else source
    summary = {k: raw[k] for k in ("id", "name", "mimeType", "version", "webViewLink", "modifiedTime") if k in raw}
    summary.update(size=_size(raw.get("size")), parents=sorted(raw.get("parents", [])),
                   labels=sorted(raw.get("labels", [])),
                   current_scope=raw.get("current_scope", scope["label"]), provenance=provenance)
    if "shortcutDetails" in raw:
        summary["shortcutDetails"] = {"targetId": raw["shortcutDetails"]["targetId"]}
    words = _name(raw["name"] + " " + " ".join(raw.get("labels", [])))
    summary["preservation_clues"] = [w for w in ("audit", "working", "certified", "signed", "trip", "original", "copy")
                                     if re.search(r"\b" + w + r"\b", words)]
    return {"summary": summary, "mime": mime, "checksums": {}, "checksum_claims": defaultdict(set),
            "checksum_sources": defaultdict(list),
            "binary_blocked": False, "exports": {}, "fingerprints": {},
            "native": mime.startswith(NATIVE_PREFIX)}


def _add_binary(record, algorithm, value, size, version, provenance, trusted, issues):
    file_id = record["summary"]["id"]

    def blocked(code, message):
        record["binary_blocked"] = True
        _issue(issues, file_id, code, message)

    if record["native"]:
        _issue(issues, file_id, "NATIVE_CHECKSUM_NOT_COMPARABLE", NATIVE_UNCERTAINTY)
        return
    if not _digest(value, algorithm):
        blocked("INVALID_CHECKSUM", "Unsupported algorithm or malformed checksum; binary comparison suppressed.")
        return
    if size is None or record["summary"]["size"] is None or size != record["summary"]["size"]:
        blocked("INVALID_OR_CONFLICTING_SIZE", "A checksum needs a valid byte size matching the inventory.")
        return
    if not _text(version) or version != record["summary"].get("version"):
        blocked("MISSING_OR_STALE_HASH_VERSION", "Checksum evidence must bind the exported file version.")
        return
    value = value.lower()
    record["checksum_claims"][algorithm].add(value)
    if len(record["checksum_claims"][algorithm]) > 1:
        blocked("CONFLICTING_CHECKSUMS", "Version-bound checksum claims disagree, including an untrusted claim; binary comparison suppressed.")
    if provenance["source"] not in trusted:
        _issue(issues, file_id, "UNTRUSTED_CHECKSUM_SOURCE", "Checksum source is absent from the explicit trusted-source allowlist.")
        return
    previous = record["checksums"].get(algorithm)
    if previous and previous != value:
        blocked("CONFLICTING_CHECKSUMS", "Sources disagree on a checksum for the same file and version.")
        return
    record["checksums"][algorithm] = value
    if provenance not in record["checksum_sources"][algorithm]:
        record["checksum_sources"][algorithm].append(provenance)


def _add_content(record, signal, source, issues):
    file_id = record["summary"]["id"]
    if not _text(signal.get("version")) or signal["version"] != record["summary"].get("version"):
        _issue(issues, file_id, "MISSING_OR_STALE_CONTENT_VERSION", "Content signal does not bind the inventory version.")
        return
    representation = signal.get("representation")
    if not _text(representation):
        _issue(issues, file_id, "MISSING_CONTENT_REPRESENTATION", "Export or fingerprint representation must be explicit.")
        return
    algorithm = signal.get("algorithm")
    if signal["kind"] == "export":
        size = _size(signal.get("size"))
        if not _digest(signal.get("value"), algorithm) or size is None:
            _issue(issues, file_id, "INVALID_EXPORT_HASH", "Export checksum or size is invalid; signal ignored.")
            return
        key, value = (algorithm, representation), (signal["value"].lower(), size)
        target = record["exports"]
    else:
        tokens = signal.get("token_hashes")
        if isinstance(tokens, list) and len(tokens) > MAX_FINGERPRINT_TOKENS:
            raise DedupError("TEXT_FINGERPRINT_TOO_LARGE")
        if (algorithm != "sha256-token-set-v1" or not isinstance(tokens, list)
                or any(not _digest(v, "sha256") for v in tokens)):
            _issue(issues, file_id, "INVALID_TEXT_FINGERPRINT", "Expected a declared SHA-256 token-set fingerprint; signal ignored.")
            return
        key, value = (algorithm, representation), frozenset(v.lower() for v in tokens)
        if len(value) < MIN_FINGERPRINT_TOKENS:
            _issue(issues, file_id, "INSUFFICIENT_TEXT_FINGERPRINT", "At least eight unique token hashes are required.")
            return
        target = record["fingerprints"]
    if key in target and target[key] is None:
        return
    if key in target and target[key]["value"] != value:
        target[key] = None
        _issue(issues, file_id, "CONFLICTING_CONTENT_SIGNALS", "Conflicting content evidence for one representation; comparison suppressed.")
    else:
        target[key] = {"value": value, "provenance": source}


def _pairs(buckets):
    pairs = set()
    examined = 0
    for values in buckets.values():
        for left, right in combinations(sorted(set(values)), 2):
            examined += 1
            if examined > MAX_PAIR_EXAMINATIONS:
                raise DedupError("TOO_MANY_PAIR_EXAMINATIONS_NARROW_EXPLICIT_SCOPE")
            pairs.add((left, right))
            if len(pairs) > MAX_CANDIDATE_PAIRS:
                raise DedupError("TOO_MANY_CANDIDATE_PAIRS_NARROW_EXPLICIT_SCOPE")
    return pairs


def build_report(inventory, ignore_config, hashes=None, *, similarity_threshold=0.85):
    """Build a deterministic review report from metadata and optional hash signals.

    Exclusions are applied before names/checksums are validated or emitted. The
    caller must supply an ignore policy, including an explicit empty list when
    appropriate. Exclusions are not learned from or written to executor journals.
    """
    excluded, trusted = _policy(ignore_config)
    if (type(similarity_threshold) not in (int, float) or not math.isfinite(similarity_threshold)
            or not 0 < similarity_threshold <= 1):
        raise DedupError("SIMILARITY_THRESHOLD_MUST_BE_GREATER_THAN_ZERO_AND_AT_MOST_ONE")
    _schema(inventory, "files")
    source = _provenance(inventory.get("provenance"))
    scope = inventory.get("scope")
    if not isinstance(scope, dict) or not _text(scope.get("label")) or type(scope.get("complete")) is not bool:
        raise DedupError("EXPLICIT_SCOPE_AND_COMPLETENESS_REQUIRED")
    scope = {"label": scope["label"], "complete": scope["complete"]}
    rows = inventory["files"]
    out_of_scope = set()
    for raw in rows:
        if not isinstance(raw, dict) or not _text(raw.get("id")):
            raise DedupError("STABLE_FILE_ID_REQUIRED")
        # Do not inspect the remaining metadata of an excluded source.
        if raw["id"] in excluded:
            continue
        if str(raw.get("review_status", "")).casefold().strip() in {"withdrawn", "excluded"}:
            excluded.add(raw["id"])
        if raw.get("in_scope") is False:
            out_of_scope.add(raw["id"])
    # Suppress shortcuts that lead, even through another shortcut, to exclusions.
    suppressed = excluded | out_of_scope
    while True:
        extra = {raw["id"] for raw in rows if raw["id"] not in suppressed
                 and isinstance(raw.get("shortcutDetails"), dict)
                 and _text(raw["shortcutDetails"].get("targetId"))
                 and raw["shortcutDetails"].get("targetId") in suppressed}
        extra.update(raw["id"] for raw in rows if raw["id"] not in suppressed
                     and _link_target(raw.get("webViewLink")) in suppressed)
        if not extra:
            break
        suppressed.update(extra)
    grouped = defaultdict(list)
    for raw in rows:
        if raw["id"] not in suppressed:
            grouped[raw["id"]].append(raw)
    records, issues, repeats = {}, [], {}
    for file_id, entries in sorted(grouped.items()):
        if any(_canonical(v) != _canonical(entries[0]) for v in entries[1:]):
            _issue(issues, file_id, "CONFLICTING_ID_RECORDS", "Multiple snapshots disagree for one stable ID; record quarantined.")
            continue
        record = _record(entries[0], source, scope)
        parents = record["summary"]["parents"]
        record["summary"]["parents"] = [parent for parent in parents if parent not in suppressed]
        record["summary"]["omitted_parent_references"] = sum(parent in suppressed for parent in parents)
        records[file_id] = record
        if len(entries) > 1:
            repeats[file_id] = len(entries)
        for field, algorithm in (("md5Checksum", "md5"), ("sha256Checksum", "sha256")):
            if field in entries[0]:
                _add_binary(record, algorithm, entries[0][field], record["summary"]["size"],
                            entries[0].get("version"), record["summary"]["provenance"], trusted, issues)
        if "size" in entries[0] and record["summary"]["size"] is None:
            record["binary_blocked"] = True
            _issue(issues, file_id, "INVALID_SIZE", "Size must be a nonnegative integer or decimal integer string.")
    ignored_signals = 0
    if hashes is not None:
        _schema(hashes, "records")
        hash_source = _provenance(hashes.get("provenance"))
        for signal in hashes["records"]:
            if not isinstance(signal, dict) or not _text(signal.get("file_id")):
                raise DedupError("HASH_RECORD_FILE_ID_REQUIRED")
            file_id = signal["file_id"]
            if file_id in suppressed or file_id not in records:
                ignored_signals += 1
                continue
            record = records[file_id]
            kind = signal.get("kind")
            allowed = {"file_id", "version", "kind", "algorithm"}
            if kind == "binary":
                allowed |= {"value", "size"}
            elif kind == "export":
                allowed |= {"value", "size", "representation"}
            elif kind == "text_fingerprint":
                allowed |= {"representation", "token_hashes"}
            if set(signal) - allowed:
                raise DedupError("UNSUPPORTED_HASH_FIELDS_METADATA_ONLY")
            if signal.get("kind") == "binary":
                _add_binary(record, signal.get("algorithm"), signal.get("value"), _size(signal.get("size")),
                            signal.get("version"), hash_source, trusted, issues)
            elif signal.get("kind") in {"export", "text_fingerprint"}:
                _add_content(record, signal, hash_source, issues)
            else:
                record["binary_blocked"] = True
                _issue(issues, file_id, "UNKNOWN_HASH_KIND", "Unknown hash kind; binary comparison suppressed.")
    findings = []

    def finding(category, ids, reason, evidence, uncertainty=()):
        if len(findings) >= MAX_FINDINGS:
            raise DedupError("TOO_MANY_FINDINGS_NARROW_EXPLICIT_SCOPE")
        ids = sorted(ids)
        key = {"category": category, "file_ids": ids, "reason": reason, "evidence": evidence}
        findings.append({"finding_id": hashlib.sha256(_canonical(key).encode()).hexdigest()[:24],
                         **key, "uncertainty": [*BASE_UNCERTAINTY, *uncertainty],
                         "recommendation": RECOMMENDATION, "full_equivalence_proven": False,
                         "action_authorized": False})

    for file_id, count in sorted(repeats.items()):
        finding("identity_reference", [file_id], "REPEATED_STABLE_ID", {"occurrences": count},
                ["Repeated inventory rows describe one object, not multiple binary copies."])
    shortcut_buckets = defaultdict(list)
    comparable = {}
    for file_id, record in records.items():
        summary = record["summary"]
        if record["mime"] == SHORTCUT:
            target = summary["shortcutDetails"]["targetId"]
            shortcut_buckets[target].append(file_id)
            ids = [file_id, target] if target in records and target != file_id else [file_id]
            finding("identity_reference", ids, "SHORTCUT_TARGET_REFERENCE", {"target_id": target},
                    ["A shortcut is a reference to its target; it is not another stored content copy.",
                     "The target may be outside this inventory; it was not fetched or followed."])
        elif record["mime"] != FOLDER:
            comparable[file_id] = record
    for target, ids in sorted(shortcut_buckets.items()):
        if len(ids) > 1:
            finding("identity_reference", ids, "SHARED_SHORTCUT_TARGET", {"target_id": target},
                    ["Several shortcuts can intentionally refer to one canonical object."])

    hash_buckets, name_buckets, link_buckets, export_buckets = (defaultdict(list) for _ in range(4))
    fingerprint_buckets = defaultdict(list)
    for file_id, record in comparable.items():
        summary = record["summary"]
        name_buckets[_copy_name(summary["name"])].append(file_id)
        if summary.get("webViewLink"):
            link_buckets[summary["webViewLink"]].append(file_id)
        if not record["binary_blocked"]:
            for algorithm, value in record["checksums"].items():
                hash_buckets[(algorithm, value)].append(file_id)
        for key, signal in record["exports"].items():
            if signal:
                export_buckets[(*key, signal["value"][0])].append(file_id)
        for key, signal in record["fingerprints"].items():
            if signal:
                for token in signal["value"]:
                    fingerprint_buckets[(*key, token)].append(file_id)

    binary_pairs = _pairs(hash_buckets)
    # Validate all shared-hash pairs before emitting any binary claims. No
    # connected-component grouping: different evidence never implies transitivity.
    for left, right in sorted(binary_pairs):
        a, b = comparable[left], comparable[right]
        mismatch = a["summary"]["size"] != b["summary"]["size"]
        conflict = any(a["checksum_claims"][k] != b["checksum_claims"][k]
                       for k in a["checksum_claims"].keys() & b["checksum_claims"].keys())
        if mismatch or conflict:
            for file_id, record in ((left, a), (right, b)):
                record["binary_blocked"] = True
                _issue(issues, file_id, "CONFLICTING_CHECKSUMS", "Shared checksum conflicts with size or another checksum; binary comparison suppressed.")
    binary_matches = set()
    for left, right in sorted(binary_pairs):
        a, b = comparable[left], comparable[right]
        if a["binary_blocked"] or b["binary_blocked"]:
            continue
        matched = {k: a["checksums"][k] for k in sorted(a["checksums"].keys() & b["checksums"].keys())
                   if a["checksums"][k] == b["checksums"][k]}
        assurance = "strong_sha256" if "sha256" in matched else "legacy_md5"
        uncertainty = ["Checksum trust is asserted by local configuration; authenticity and freshness are not independently verified."]
        if assurance == "legacy_md5":
            uncertainty.append("MD5 is collision-prone. This is lower-assurance byte-match evidence, not cryptographic proof; verify SHA-256 independently.")
        if a["summary"]["mimeType"] != b["summary"]["mimeType"]:
            uncertainty.append("MIME labels differ and need review even when byte checksums match.")
        evidence = {"assurance": assurance, "checksums": matched, "size": a["summary"]["size"],
                    "versions": {k: records[k]["summary"]["version"] for k in (left, right)},
                    "sources": {k: {algo: sorted(records[k]["checksum_sources"][algo], key=_canonical)
                                    for algo in matched} for k in (left, right)}}
        finding("binary_checksum_match", [left, right], "MATCHING_TRUSTED_BINARY_CHECKSUM_AND_SIZE", evidence, uncertainty)
        binary_matches.add((left, right))
    for left, right in sorted(_pairs(name_buckets)):
        if (left, right) in binary_matches:
            continue
        a, b = comparable[left]["summary"], comparable[right]["summary"]
        reason = "SAME_NORMALIZED_NAME" if _name(a["name"]) == _name(b["name"]) else "COPY_PREFIX_NAME_CLUE"
        finding("same_name_candidate", [left, right], reason, {"normalization": "Unicode NFKC, case and whitespace; Copy of prefix only"},
                ["Names, Original/Copy labels, MIME and folder placement are clues only. Repeated trip plans or distinct versions can legitimately share names."])
    for left, right in sorted(_pairs(link_buckets)):
        finding("reference_candidate", [left, right], "SHARED_LINK_DISTINCT_IDS", {"shared_link": comparable[left]["summary"]["webViewLink"]},
                ["A matching canonical link does not establish that distinct stable IDs are duplicate files. Verify the reference metadata manually."])
    for left, right in sorted(_pairs(export_buckets)):
        a, b = comparable[left], comparable[right]
        matches = []
        conflict = False
        for key in sorted(a["exports"].keys() & b["exports"].keys()):
            x, y = a["exports"][key], b["exports"][key]
            if x and y and x["value"] != y["value"]:
                conflict = True
            if x and y and x["value"] == y["value"]:
                matches.append({"algorithm": key[0], "representation": key[1], "checksum": x["value"][0],
                                "export_size": x["value"][1], "sources": [x["provenance"], y["provenance"]]})
        if conflict:
            for file_id in (left, right):
                _issue(issues, file_id, "CONFLICTING_EXPORT_HASHES", "An export hash match conflicts with a size or another comparable export hash; export match suppressed.")
        elif matches:
            finding("content_similar_candidate", [left, right], "MATCHING_EXPORT_HASH", {"matches": matches},
                    [NATIVE_UNCERTAINTY, "Export provenance is supplied evidence, not an authentication or current-content check."])
    for left, right in sorted(_pairs(fingerprint_buckets)):
        a, b = comparable[left], comparable[right]
        for key in sorted(a["fingerprints"].keys() & b["fingerprints"].keys()):
            x, y = a["fingerprints"][key], b["fingerprints"][key]
            if not x or not y:
                continue
            shared = len(x["value"] & y["value"])
            total = len(x["value"] | y["value"])
            score = shared / total
            if score >= similarity_threshold:
                finding("content_similar_candidate", [left, right], "SIMILAR_SUPPLIED_TEXT_FINGERPRINT",
                        {"algorithm": key[0], "representation": key[1], "jaccard": score,
                         "shared_tokens": shared, "union_tokens": total, "threshold": similarity_threshold,
                         "sources": [x["provenance"], y["provenance"]]},
                        ["Token sets lose order and repetition. Boilerplate, templates and repeated trip plans may match without being duplicates.",
                         "No text bodies were read. Supplied fingerprints can be stale or incorrect.", NATIVE_UNCERTAINTY])
    findings.sort(key=lambda f: (f["category"], f["file_ids"], f["reason"], f["finding_id"]))
    issues.sort(key=lambda v: (v["file_id"], v["code"], v["message"]))
    counts = {category: sum(f["category"] == category for f in findings) for category in sorted({f["category"] for f in findings})}
    return {"schema_version": 1, "mode": "report_only", "action_authorized": False,
            "full_equivalence_proven": False, "provenance": source, "scope": scope,
            "limitations": [*BASE_UNCERTAINTY, NATIVE_UNCERTAINTY,
                            "Coverage is limited to supplied metadata, hashes and explicit scope; absent matches are not proof of uniqueness.",
                            "Exclusion totals include suppressed shortcut references; their identities and metadata are omitted."],
            "summary": {"input_records": len(rows), "eligible_records": len(records),
                        "excluded_records": sum(r["id"] in suppressed for r in rows),
                        "quarantined_ids": len(grouped) - len(records), "ignored_hash_records": ignored_signals,
                        "findings_by_category": counts, "issues": len(issues)},
            "records": [record["summary"] for record in records.values()], "findings": findings, "issues": issues}


def _csv_safe(value):
    value = str(value)
    # CSV quoting does not prevent spreadsheets interpreting formulas.
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _csv_report(report):
    buffer = io.StringIO(newline="")
    fields = ["finding_id", "category", "file_ids", "names", "reason", "evidence", "uncertainty", "recommendation", "metadata"]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    names = {r["id"]: r["name"] for r in report["records"]}
    metadata = {r["id"]: r for r in report["records"]}
    for finding in report["findings"]:
        values = {k: finding[k] for k in ("finding_id", "category", "reason", "recommendation")}
        values.update(file_ids=" | ".join(finding["file_ids"]),
                      names=" | ".join(names.get(v, "[target outside inventory]") for v in finding["file_ids"]),
                      evidence=_canonical(finding["evidence"]), uncertainty=" | ".join(finding["uncertainty"]),
                      metadata=_canonical([metadata[v] for v in finding["file_ids"] if v in metadata]))
        writer.writerow({k: _csv_safe(v) for k, v in values.items()})
    # CSV is independently useful: validation issues cannot disappear from it.
    for issue in report["issues"]:
        writer.writerow({k: _csv_safe(v) for k, v in {
            "category": "validation_issue", "file_ids": issue["file_id"], "names": names.get(issue["file_id"], "[quarantined]"),
            "reason": issue["code"], "evidence": issue["message"], "uncertainty": "Comparison evidence is incomplete or conflicting.",
            "recommendation": RECOMMENDATION}.items()})
    return buffer.getvalue()


def write_reports(report, output_dir):
    """Create a fresh report directory and owner-readable files. Never overwrite.

    A partial write is deliberately retained on failure and must be inspected;
    a retry must select a fresh output directory. No cleanup deletes are used.
    """
    output = Path(output_dir)
    payloads = {"json": (json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode(),
                "csv": _csv_report(report).encode("utf-8")}
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = {}
    for kind, payload in payloads.items():
        path = output / ("report.dedup-report." + kind)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        result[kind] = str(path.absolute())
    directory = os.open(output, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return result


def _load(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DedupError("DUPLICATE_JSON_OBJECT_KEY")
            result[key] = value
        return result

    def reject_constant(_):
        raise DedupError("NONFINITE_JSON_NUMBER")

    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=unique_keys, parse_constant=reject_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path, help="Offline metadata JSON manifest (no bodies)")
    parser.add_argument("--ignore-config", required=True, type=Path, help="Private exclusions and trusted checksum sources")
    parser.add_argument("--hashes", type=Path, help="Optional precomputed, version-bound hash/fingerprint JSON")
    parser.add_argument("--output-dir", type=Path, help="New output directory (default: private/dedup-reports/<unique ID>)")
    parser.add_argument("--similarity-threshold", type=float, default=0.85)
    args = parser.parse_args(argv)
    try:
        config = _load(args.ignore_config)
        _policy(config)  # Validate policy before opening the inventory or sidecar.
        report = build_report(_load(args.inventory), config, _load(args.hashes) if args.hashes else None,
                              similarity_threshold=args.similarity_threshold)
        output = args.output_dir or Path("private") / "dedup-reports" / str(uuid4())
        paths = write_reports(report, output)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        # Do not echo raw paths, JSON content, private IDs or names to the terminal.
        code = str(error) if isinstance(error, DedupError) else type(error).__name__
        parser.exit(2, "Report blocked: " + code + ". No Drive action occurred; inspect any partial local output.\n")
    print(json.dumps({"mode": "report_only", "summary": report["summary"], "outputs": paths}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
