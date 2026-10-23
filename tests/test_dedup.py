"""Synthetic-only contract tests for offline, report-only duplicate review."""
import copy
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from uqmc_recovery.dedup import DedupError, build_report, main, write_reports


STAMP = "2026-10-21T00:00:00Z"
SHA_A = "a" * 64
SHA_B = "b" * 64
MD5_A = "a" * 32
MD5_B = "b" * 32
NATIVE = "application/vnd.google-apps."


def file_record(file_id, name=None, mime="application/pdf", **extra):
    record = {
        "id": file_id, "name": name or file_id + ".pdf", "mimeType": mime,
        "size": "100", "version": "1", "parents": ["fixture-root"],
        "current_scope": "fixture-root", "labels": [],
    }
    record.update(extra)
    return record


def inventory(*records):
    return {
        "schema_version": 1,
        "provenance": {"source": "fixture-export", "reference": "synthetic",
                       "captured_at": STAMP},
        "scope": {"label": "synthetic root", "complete": False},
        "files": list(records),
    }


def policy(**extra):
    value = {"excluded_ids": [],
             "trusted_checksum_sources": ["fixture-export", "fixture-hashes"]}
    value.update(extra)
    return value


def sidecar(*records):
    return {
        "schema_version": 1,
        "provenance": {"source": "fixture-hashes", "reference": "synthetic",
                       "captured_at": STAMP},
        "records": list(records),
    }


def binary_hash(file_id, value=SHA_A, **extra):
    record = {"file_id": file_id, "version": "1", "kind": "binary",
              "algorithm": "sha256", "value": value, "size": 100}
    record.update(extra)
    return record


def token_hashes(*tokens):
    return [hashlib.sha256(str(token).encode()).hexdigest() for token in tokens]


def fingerprint(file_id, tokens, **extra):
    record = {"file_id": file_id, "version": "1", "kind": "text_fingerprint",
              "algorithm": "sha256-token-set-v1", "representation": "text/plain-v1",
              "token_hashes": token_hashes(*tokens)}
    record.update(extra)
    return record


def findings(report, category):
    return [item for item in report["findings"] if item["category"] == category]


class DedupTests(unittest.TestCase):
    def binary_matches(self, report):
        return findings(report, "binary_checksum_match")

    def test_matching_export_checksum_with_conflicting_size_is_reported(self):
        source = inventory(file_record("fixture-a", mime=NATIVE + "document"),
                           file_record("fixture-b", mime=NATIVE + "document"))
        signals = sidecar(*[
            {"file_id": file_id, "version": "1", "kind": "export", "algorithm": "sha256",
             "value": SHA_A, "size": size, "representation": "text/plain-v1"}
            for file_id, size in (("fixture-a", 100), ("fixture-b", 101))
        ])
        report = build_report(source, policy(), signals)
        self.assertEqual(findings(report, "content_similar_candidate"), [])
        self.assertEqual({issue["file_id"] for issue in report["issues"]}, {"fixture-a", "fixture-b"})

    def test_paths_urls_and_root_alias_cannot_substitute_for_stable_ids(self):
        for identifier in ("root", "folder/file", "https://drive.google.com/file/d/fixture/view"):
            with self.subTest(identifier=identifier), self.assertRaises(DedupError):
                build_report(inventory(file_record(identifier)), policy())

    def test_trusted_sha256_and_size_report_bytes_not_full_equivalence(self):
        report = build_report(inventory(
            file_record("fixture-a", sha256Checksum=SHA_A),
            file_record("fixture-b", sha256Checksum=SHA_A)), policy())
        self.assertEqual(report["mode"], "report_only")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["summary"]["input_records"], 2)
        self.assertEqual(report["summary"]["eligible_records"], 2)
        matches = self.binary_matches(report)
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(set(match["file_ids"]), {"fixture-a", "fixture-b"})
        self.assertEqual(match["evidence"]["assurance"], "strong_sha256")
        self.assertIs(match["full_equivalence_proven"], False)
        self.assertIs(match["action_authorized"], False)
        self.assertTrue(match["uncertainty"])
        self.assertTrue(match["reason"])
        self.assertIn("review", match["recommendation"].lower())

    def test_legacy_md5_has_distinct_lower_assurance(self):
        report = build_report(inventory(
            file_record("fixture-a", md5Checksum=MD5_A),
            file_record("fixture-b", md5Checksum=MD5_A)), policy())
        matches = self.binary_matches(report)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["evidence"]["assurance"], "legacy_md5")
        self.assertIn("collision", json.dumps(matches[0]).lower())

    def test_untrusted_checksum_sources_cannot_confirm_binary_match(self):
        report = build_report(inventory(
            file_record("fixture-a", sha256Checksum=SHA_A),
            file_record("fixture-b", sha256Checksum=SHA_A)),
            policy(trusted_checksum_sources=[]))
        self.assertEqual(self.binary_matches(report), [])

    def test_supplied_binary_hashes_bind_to_file_version_and_size(self):
        source = inventory(file_record("fixture-a"), file_record("fixture-b"))
        hashes = sidecar(binary_hash("fixture-a"), binary_hash("fixture-b"))
        report = build_report(source, policy(), hashes)
        self.assertEqual(len(self.binary_matches(report)), 1)
        hashes["records"][1]["version"] = "old-version"
        report = build_report(source, policy(), hashes)
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(report["issues"])

    def test_supplied_hash_size_mismatch_quarantines_binary_evidence(self):
        report = build_report(inventory(file_record("fixture-a"), file_record("fixture-b")),
                              policy(), sidecar(binary_hash("fixture-a"),
                                                binary_hash("fixture-b", size=101)))
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(report["issues"])

    def test_missing_size_or_version_does_not_confirm_binary_match(self):
        for missing in ("size", "version"):
            with self.subTest(missing=missing):
                first = file_record("fixture-a", sha256Checksum=SHA_A)
                first.pop(missing)
                report = build_report(inventory(first, file_record("fixture-b", sha256Checksum=SHA_A)), policy())
                self.assertEqual(self.binary_matches(report), [])

    def test_same_hash_with_different_sizes_is_not_a_binary_match(self):
        report = build_report(inventory(
            file_record("fixture-a", sha256Checksum=SHA_A),
            file_record("fixture-b", sha256Checksum=SHA_A, size="101")), policy())
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(report["issues"])

    def test_equal_md5_cannot_override_conflicting_sha256(self):
        report = build_report(inventory(
            file_record("fixture-a", md5Checksum=MD5_A, sha256Checksum=SHA_A),
            file_record("fixture-b", md5Checksum=MD5_A, sha256Checksum=SHA_B)), policy())
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(report["issues"])

    def test_conflicting_hashes_for_same_record_quarantine_it(self):
        report = build_report(inventory(
            file_record("fixture-a", sha256Checksum=SHA_A),
            file_record("fixture-b", sha256Checksum=SHA_A)), policy(),
            sidecar(binary_hash("fixture-a", value=SHA_B)))
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(report["issues"])

    def test_malformed_hash_does_not_fall_back_to_other_valid_checksum(self):
        for malformed in ("not-hex", "g" * 64, "a" * 63, 123):
            with self.subTest(value=malformed):
                report = build_report(inventory(
                    file_record("fixture-a", md5Checksum=MD5_A, sha256Checksum=malformed),
                    file_record("fixture-b", md5Checksum=MD5_A, sha256Checksum=SHA_A)), policy())
                self.assertEqual(self.binary_matches(report), [])
                self.assertTrue(report["issues"])

    def test_invalid_sizes_do_not_confirm_binary_matches(self):
        for invalid in (-1, True, "1.5", "unknown"):
            with self.subTest(size=invalid):
                report = build_report(inventory(
                    file_record("fixture-a", sha256Checksum=SHA_A, size=invalid),
                    file_record("fixture-b", sha256Checksum=SHA_A)), policy())
                self.assertEqual(self.binary_matches(report), [])

    def test_all_google_native_mime_families_are_ineligible_for_binary_equality(self):
        for kind in ("document", "spreadsheet", "presentation", "drawing", "form",
                     "script", "map", "site", "folder", "shortcut", "unknown-native"):
            with self.subTest(kind=kind):
                extra = {"shortcutDetails": {"targetId": "fixture-target"}} if kind == "shortcut" else {}
                report = build_report(inventory(
                    file_record("fixture-a", mime=NATIVE + kind, sha256Checksum=SHA_A, **extra),
                    file_record("fixture-b", mime=NATIVE + kind, sha256Checksum=SHA_A, **extra)),
                    policy(), sidecar(binary_hash("fixture-a"), binary_hash("fixture-b")))
                self.assertEqual(self.binary_matches(report), [])

    def test_noncanonical_native_mime_cannot_bypass_binary_guard(self):
        for mime in ("APPLICATION/VND.GOOGLE-APPS.DOCUMENT",
                     " application/vnd.google-apps.document",
                     "Application/Vnd.Google-Apps.Spreadsheet ",
                     "\tAPPLICATION/VND.GOOGLE-APPS.PRESENTATION\t"):
            with self.subTest(mime=mime):
                try:
                    report = build_report(inventory(
                        file_record("fixture-a", mime=mime, sha256Checksum=SHA_A),
                        file_record("fixture-b", mime=mime, sha256Checksum=SHA_A)), policy())
                except DedupError:
                    # A strict rejection or canonicalization is safe.
                    continue
                self.assertEqual(self.binary_matches(report), [])

    def test_sidecar_rejects_body_and_undeclared_fields(self):
        for field in ("body", "text", "local_path", "provenance"):
            with self.subTest(field=field), self.assertRaises(DedupError):
                build_report(inventory(file_record("fixture-a")), policy(),
                             sidecar(binary_hash("fixture-a", **{field: "fixture-only"})))

    def test_sidecar_excluded_or_unknown_ids_skip_undeclared_fields(self):
        for field in ("body", "text", "local_path", "provenance"):
            with self.subTest(field=field):
                hashes = sidecar(binary_hash("fixture-withheld", **{field: "HIDDEN-PAYLOAD"}),
                                 binary_hash("fixture-unknown", **{field: "HIDDEN-PAYLOAD"}))
                report = build_report(inventory(file_record("fixture-a")),
                                      policy(excluded_ids=["fixture-withheld"]), hashes)
                for secret in ("fixture-withheld", "fixture-unknown", "HIDDEN-PAYLOAD"):
                    self.assertNotIn(secret, json.dumps(report))

    def test_untrusted_contrary_version_bound_hash_blocks_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                source = inventory(file_record("fixture-a", sha256Checksum=SHA_A),
                                   file_record("fixture-b", sha256Checksum=SHA_A))
                signals = [binary_hash("fixture-a", value=SHA_A),
                           binary_hash("fixture-a", value=SHA_B)]
                if reverse:
                    signals.reverse()
                hashes = sidecar(*signals)
                hashes["provenance"]["source"] = "fixture-untrusted"
                report = build_report(source, policy(), hashes)
                self.assertEqual(self.binary_matches(report), [])
                self.assertTrue(report["issues"])

    def test_untrusted_inventory_conflict_cannot_be_overridden_by_trusted_sidecar(self):
        source = inventory(file_record("fixture-a", sha256Checksum=SHA_B),
                           file_record("fixture-b", sha256Checksum=SHA_A))
        source["files"][0]["provenance"] = {
            "source": "fixture-untrusted", "reference": "synthetic", "captured_at": STAMP}
        report = build_report(source, policy(), sidecar(binary_hash("fixture-a"), binary_hash("fixture-b")))
        self.assertEqual(self.binary_matches(report), [])

    def test_equal_native_exports_remain_uncertain_content_candidates(self):
        report = build_report(inventory(
            file_record("fixture-a", mime=NATIVE + "document"),
            file_record("fixture-b", mime=NATIVE + "document")), policy(),
            sidecar(binary_hash("fixture-a", kind="export", representation="text/plain-v1"),
                    binary_hash("fixture-b", kind="export", representation="text/plain-v1")))
        self.assertEqual(self.binary_matches(report), [])
        candidates = findings(report, "content_similar_candidate")
        self.assertTrue(candidates)
        self.assertTrue(candidates[0]["uncertainty"])
        self.assertIn("export", json.dumps(candidates).lower())

    def test_same_name_and_same_size_are_only_candidates(self):
        report = build_report(inventory(file_record("fixture-a", "Trip plan.pdf"),
                                        file_record("fixture-b", "Trip plan.pdf")), policy())
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(findings(report, "same_name_candidate"))

    def test_copy_of_label_is_only_a_name_clue(self):
        report = build_report(inventory(file_record("fixture-a", "Trip plan.pdf"),
                                        file_record("fixture-b", "Copy of Trip plan.pdf")), policy())
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(findings(report, "same_name_candidate"))

    def test_repeated_trip_names_with_different_hashes_remain_name_candidates(self):
        report = build_report(inventory(
            file_record("fixture-a", "Trip plan.pdf", sha256Checksum=SHA_A),
            file_record("fixture-b", "Trip plan.pdf", sha256Checksum=SHA_B)), policy())
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(findings(report, "same_name_candidate"))

    def test_audit_certified_and_working_labels_do_not_choose_a_winner(self):
        report = build_report(inventory(
            file_record("fixture-a", "Original audit copy.pdf", sha256Checksum=SHA_A, labels=["audit", "certified"]),
            file_record("fixture-b", "Working copy.pdf", sha256Checksum=SHA_A, labels=["working"])), policy())
        self.assertTrue(self.binary_matches(report))
        match = self.binary_matches(report)[0]
        self.assertIs(match["action_authorized"], False)
        self.assertIs(match["full_equivalence_proven"], False)
        self.assertNotIn("canonical_id", match)
        self.assertNotIn("delete_ids", match)
        self.assertNotIn("keep_id", match)
        self.assertIn("review", match["recommendation"].lower())

    def test_repeated_stable_id_is_an_identity_reference(self):
        record = file_record("fixture-a", sha256Checksum=SHA_A)
        report = build_report(inventory(record, copy.deepcopy(record)), policy())
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(findings(report, "identity_reference"))
        self.assertEqual(len(report["records"]), 1)

    def test_conflicting_repeated_id_is_quarantined(self):
        first = file_record("fixture-a", sha256Checksum=SHA_A)
        second = file_record("fixture-a", sha256Checksum=SHA_B, version="2")
        report = build_report(inventory(first, second,
                                       file_record("fixture-b", sha256Checksum=SHA_A)), policy())
        self.assertEqual(self.binary_matches(report), [])
        self.assertIn("CONFLICTING_ID_RECORDS", [issue["code"] for issue in report["issues"]])

    def test_equal_canonical_links_are_references_not_duplicate_evidence(self):
        link = "https://drive.google.com/file/d/fixture-target/view"
        report = build_report(inventory(file_record("fixture-a", webViewLink=link),
                                        file_record("fixture-b", webViewLink=link)), policy())
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(findings(report, "reference_candidate"))

    def test_shortcuts_to_same_target_are_identity_references(self):
        report = build_report(inventory(
            file_record("fixture-a", mime=NATIVE + "shortcut", shortcutDetails={"targetId": "fixture-target"}),
            file_record("fixture-b", mime=NATIVE + "shortcut", shortcutDetails={"targetId": "fixture-target"})), policy())
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(findings(report, "identity_reference"))

    def test_each_private_exclusion_list_suppresses_records_before_validation(self):
        for key in ("excluded_ids", "withdrawn_ids", "denied_source_ids"):
            with self.subTest(key=key):
                config = {key: ["fixture-withheld"], "trusted_checksum_sources": ["fixture-export"]}
                # An excluded record intentionally lacks otherwise required metadata.
                source = inventory({"id": "fixture-withheld", "name": "HIDDEN-NAME", "sha256Checksum": 123},
                                   file_record("fixture-public"))
                hashes = sidecar({"file_id": "fixture-withheld", "value": "HIDDEN-HASH"})
                report = build_report(source, config, hashes)
                serialized = json.dumps(report)
                self.assertNotIn("fixture-withheld", serialized)
                self.assertNotIn("HIDDEN-NAME", serialized)
                self.assertNotIn("HIDDEN-HASH", serialized)
                self.assertEqual(report["summary"]["excluded_records"], 1)

    def test_excluded_shortcut_target_propagates_through_reference_chain(self):
        source = inventory(
            {"id": "fixture-withheld", "name": "HIDDEN-TARGET"},
            file_record("fixture-link-a", "HIDDEN-LINK-A", mime=NATIVE + "shortcut",
                        shortcutDetails={"targetId": "fixture-withheld"}),
            file_record("fixture-link-b", "HIDDEN-LINK-B", mime=NATIVE + "shortcut",
                        shortcutDetails={"targetId": "fixture-link-a"}),
            file_record("fixture-public"))
        report = build_report(source, policy(excluded_ids=["fixture-withheld"]))
        serialized = json.dumps(report)
        for secret in ("fixture-withheld", "fixture-link-a", "fixture-link-b", "HIDDEN-"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(report["summary"]["excluded_records"], 3)

    def test_excluded_structured_parent_ids_are_not_emitted(self):
        source = inventory(file_record("fixture-a", parents=["fixture-withheld", "fixture-other"]))
        original = copy.deepcopy(source)
        report = build_report(source, policy(excluded_ids=["fixture-withheld"]))
        self.assertNotIn("fixture-withheld", json.dumps(report))
        self.assertEqual(report["records"][0]["parents"], ["fixture-other"])
        self.assertEqual(source, original)

    def test_canonical_google_link_aliases_to_excluded_targets_are_suppressed(self):
        for link in ("https://drive.google.com/file/d/fixture-withheld/view?usp=sharing",
                     "https://drive.google.com/open?id=fixture-withheld",
                     "https://docs.google.com/document/d/fixture-withheld/edit",
                     "https://docs.google.com/spreadsheets/d/fixture-withheld/edit#gid=0",
                     "https://docs.google.com/presentation/d/fixture-withheld/edit"):
            with self.subTest(link=link):
                report = build_report(inventory(file_record("fixture-link", "HIDDEN-LINK", webViewLink=link),
                                                file_record("fixture-public")),
                                      policy(excluded_ids=["fixture-withheld"]))
                serialized = json.dumps(report)
                for secret in ("fixture-link", "fixture-withheld", "HIDDEN-LINK"):
                    self.assertNotIn(secret, serialized)
                self.assertEqual([r["id"] for r in report["records"]], ["fixture-public"])

    def test_record_withdrawal_and_current_scope_are_respected(self):
        source = inventory(file_record("fixture-withheld", review_status="withdrawn"),
                           file_record("fixture-excluded", review_status="excluded"),
                           file_record("fixture-outside", in_scope=False),
                           file_record("fixture-public"))
        report = build_report(source, policy())
        self.assertEqual([record["id"] for record in report["records"]], ["fixture-public"])
        self.assertNotIn("fixture-withheld", json.dumps(report))
        self.assertNotIn("fixture-excluded", json.dumps(report))

    def test_ignore_config_is_required_and_malformed_lists_fail_closed(self):
        source = inventory(file_record("fixture-a"))
        for config in (None, {}, {"excluded_ids": "fixture-a"}, {"excluded_ids": [123]}):
            with self.subTest(config=config), self.assertRaises(DedupError):
                build_report(source, config)

    def test_sufficient_text_fingerprints_report_similarity_without_tokens(self):
        common = list(range(20))
        first = fingerprint("fixture-a", common + [101])
        second = fingerprint("fixture-b", common + [102])
        report = build_report(inventory(file_record("fixture-a"), file_record("fixture-b")),
                              policy(), sidecar(first, second))
        self.assertEqual(self.binary_matches(report), [])
        self.assertTrue(findings(report, "content_similar_candidate"))
        self.assertNotIn(first["token_hashes"][0], json.dumps(report))

    def test_short_empty_or_repeated_fingerprint_tokens_are_not_sufficient(self):
        for tokens in ([], list(range(7)), [1] * 10):
            with self.subTest(tokens=tokens):
                report = build_report(inventory(file_record("fixture-a"), file_record("fixture-b")),
                                      policy(), sidecar(fingerprint("fixture-a", tokens),
                                                        fingerprint("fixture-b", tokens)))
                self.assertEqual(findings(report, "content_similar_candidate"), [])

    def test_fingerprint_representation_and_threshold_must_match(self):
        source = inventory(file_record("fixture-a"), file_record("fixture-b"))
        hashes = sidecar(fingerprint("fixture-a", range(10)),
                         fingerprint("fixture-b", range(10), representation="other-text-v1"))
        self.assertEqual(findings(build_report(source, policy(), hashes), "content_similar_candidate"), [])
        hashes["records"][1] = fingerprint("fixture-b", range(1, 11))
        report = build_report(source, policy(), hashes, similarity_threshold=0.85)
        self.assertEqual(findings(report, "content_similar_candidate"), [])

    def test_invalid_similarity_threshold_fails_closed(self):
        for threshold in (-0.1, 1.1, float("nan"), float("inf"), True):
            with self.subTest(threshold=threshold), self.assertRaises(DedupError):
                build_report(inventory(), policy(), similarity_threshold=threshold)

    def test_results_are_deterministic_and_inputs_unchanged(self):
        source = inventory(file_record("fixture-b", "Shared.pdf", sha256Checksum=SHA_A),
                           file_record("fixture-a", "Shared.pdf", sha256Checksum=SHA_A))
        config = policy()
        original_source, original_config = copy.deepcopy(source), copy.deepcopy(config)
        first = build_report(source, config)
        second = build_report(source, config)
        self.assertEqual(first, second)
        self.assertEqual(source, original_source)
        self.assertEqual(config, original_config)
        source["files"].reverse()
        reordered = build_report(source, config)
        self.assertEqual(first["findings"], reordered["findings"])

    def test_mixed_algorithm_pair_evidence_does_not_create_transitive_equivalence(self):
        report = build_report(inventory(
            file_record("fixture-a", md5Checksum=MD5_A),
            file_record("fixture-b", md5Checksum=MD5_A, sha256Checksum=SHA_A),
            file_record("fixture-c", sha256Checksum=SHA_A)), policy())
        for match in self.binary_matches(report):
            self.assertFalse({"fixture-a", "fixture-c"} <= set(match["file_ids"]))

    def test_comparison_budget_exhaustion_fails_closed(self):
        source = inventory(*(file_record("fixture-" + str(i), "Shared.pdf") for i in range(3)))
        for limit in ("MAX_PAIR_EXAMINATIONS", "MAX_CANDIDATE_PAIRS", "MAX_FINDINGS"):
            with self.subTest(limit=limit), mock.patch("uqmc_recovery.dedup." + limit, 2):
                with self.assertRaises(DedupError):
                    build_report(source, policy())

    def test_oversized_fingerprint_fails_closed_at_declared_budget(self):
        with mock.patch("uqmc_recovery.dedup.MAX_FINGERPRINT_TOKENS", 8):
            with self.assertRaises(DedupError):
                build_report(inventory(file_record("fixture-a")), policy(),
                             sidecar(fingerprint("fixture-a", range(9))))


class DedupReportOutputTests(unittest.TestCase):
    def test_json_and_csv_reports_are_private_and_cannot_overwrite(self):
        report = build_report(inventory(file_record("fixture-a", "Shared.pdf"),
                                        file_record("fixture-b", "Shared.pdf")), policy())
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "private-output"
            paths = write_reports(report, output)
            self.assertEqual(set(paths), {"json", "csv"})
            json_path, csv_path = Path(paths["json"]), Path(paths["csv"])
            self.assertEqual(json_path.name, "report.dedup-report.json")
            self.assertEqual(csv_path.name, "report.dedup-report.csv")
            self.assertEqual(json.loads(json_path.read_text()), report)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertTrue({"finding_id", "category", "file_ids", "names", "reason", "evidence",
                             "uncertainty", "recommendation"} <= rows[0].keys())
            original_json = json_path.read_bytes()
            original_csv = csv_path.read_bytes()
            with self.assertRaises((FileExistsError, DedupError)):
                write_reports(report, output)
            self.assertEqual(json_path.read_bytes(), original_json)
            self.assertEqual(csv_path.read_bytes(), original_csv)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(json_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(csv_path.stat().st_mode), 0o600)

    def test_csv_cells_do_not_execute_names_as_formulas(self):
        for name in ("=1+1", "+1+1", "-1+1", "@SUM(1,2)", "\t=1+1", "\r=1+1"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                report = build_report(inventory(file_record("fixture-a", name),
                                                file_record("fixture-b", name)), policy())
                paths = write_reports(report, Path(tmp) / "reports")
                with open(paths["csv"], newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertTrue(rows)
                for row in rows:
                    for cell in row.values():
                        self.assertFalse(cell.lstrip().startswith(("=", "+", "-", "@")), cell)


class DedupCliTests(unittest.TestCase):
    def cli(self, tmp, *arguments, script=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        command = [sys.executable, "-c", script] if script is not None else [sys.executable, "-m", "uqmc_recovery.dedup", *arguments]
        return subprocess.run(command, cwd=tmp, env=env, capture_output=True, text=True, timeout=15)

    def save_inputs(self, tmp):
        root = Path(tmp)
        source = root / "fixture-inventory.json"
        config = root / "fixture-ignore.json"
        source.write_text(json.dumps(inventory(file_record("fixture-a", "Shared.pdf"),
                                               file_record("fixture-b", "Shared.pdf"))))
        config.write_text(json.dumps(policy()))
        return source, config

    def test_ignore_config_is_required_by_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.cli(tmp, "--inventory", "fixture-does-not-exist.json")
            self.assertEqual(result.returncode, 2)
            self.assertIn("--ignore-config", result.stderr)
            self.assertFalse((Path(tmp) / "private").exists())

    def test_invalid_policy_is_checked_before_opening_inventory(self):
        with mock.patch("uqmc_recovery.dedup._load", return_value={}) as load:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["--inventory", "fixture-unopened.json", "--ignore-config", "fixture-policy.json"])
        self.assertEqual(error.exception.code, 2)
        load.assert_called_once_with(Path("fixture-policy.json"))

    def test_cli_rejects_malformed_and_duplicate_json_without_private_diagnostics(self):
        invalid_inputs = ('{"schema_version":',
                          '{"schema_version":1,"schema_version":1,"files":[]}',
                          '{"private-marker":NaN}')
        with tempfile.TemporaryDirectory() as tmp:
            source, config = self.save_inputs(tmp)
            for text in invalid_inputs:
                with self.subTest(text=text):
                    source.write_text(text)
                    result = self.cli(tmp, "--inventory", str(source), "--ignore-config", str(config))
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("Report blocked:", result.stderr)
                    self.assertNotIn("private-marker", result.stderr)
                    self.assertNotIn(str(source), result.stderr)
                    self.assertFalse((Path(tmp) / "private").exists())

    def test_cli_default_and_explicit_new_output_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, config = self.save_inputs(tmp)
            args = ("--inventory", str(source), "--ignore-config", str(config))
            default_run = self.cli(tmp, *args)
            self.assertEqual(default_run.returncode, 0, default_run.stderr)
            result = json.loads(default_run.stdout)
            for path in result["outputs"].values():
                self.assertTrue(Path(path).is_file())
                self.assertTrue(Path(path).resolve().is_relative_to(
                    (Path(tmp) / "private" / "dedup-reports").resolve()))
            output = Path(tmp) / "explicit-output"
            first = self.cli(tmp, *args, "--output-dir", str(output))
            self.assertEqual(first.returncode, 0, first.stderr)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            retry = self.cli(tmp, *args, "--output-dir", str(output))
            self.assertEqual(retry.returncode, 2)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})

    def test_cli_needs_no_network_credentials_or_executor_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, config = self.save_inputs(tmp)
            script = '''
import socket
import sys
from unittest.mock import patch
with patch.object(socket, "socket", side_effect=AssertionError("Network forbidden")), \\
     patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")):
    from uqmc_recovery.dedup import main
    main(["--inventory", %r, "--ignore-config", %r])
for module in ("uqmc_recovery.engine", "uqmc_recovery.adapters", "uqmc_recovery.journal", "uqmc_recovery.model", "uqmc_recovery.__main__"):
    assert module not in sys.modules, module
''' % (str(source), str(config))
            result = self.cli(tmp, script=script)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
