"""Run-cache safety and cost checks using synthetic sections and local fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch

import test_section_agent_profile as fixtures
from uqmc_recovery.model import SafetyError
from uqmc_recovery.run_authority import RunAuthority
from uqmc_recovery.section_importer import validate_section_authority


class RunAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SectionAgentProfileIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        original_approval = self.fixture.fixture.approval
        self.section_expiry = datetime.now(timezone.utc) + timedelta(hours=2)
        self.fixture.fixture.approval = lambda **kwargs: original_approval(
            expires_at_utc=self.section_expiry.isoformat(), **kwargs)
        self.fixture._enabled_section_handoff()
        self.journal, self.plan = self.fixture.journal, self.fixture.plan
        self.plan_hash = self.plan.proposal_hash
        self.auth_event = next(event for event in reversed(self.journal.records()) if event["kind"] == "HANDOFF_AUTHORIZED")
        self.auth_ref = self.auth_event["payload"]["authorization_ref"]
        self.auth_path = self.journal.evidence_dir / (self.auth_ref[7:] + ".json")

    def cache(self, audit_permissions=False):
        return RunAuthority(self.journal, self.plan, self.plan_hash, audit_permissions)

    def test_full_validation_once_then_tail_and_stats_only(self):
        with self.journal.locked(), patch("uqmc_recovery.run_authority.validate_section_authority",
                wraps=validate_section_authority) as validate:
            cache = self.cache()
            with patch.object(self.journal, "records", side_effect=AssertionError("full records read")), \
                    patch.object(self.journal, "read_evidence", side_effect=AssertionError("evidence reread")), \
                    patch.object(self.journal, "records_after", wraps=self.journal.records_after) as tail:
                first = cache.check()
                first["authorized"] = False
                self.assertTrue(cache.check()["authorized"])
                self.assertEqual(tail.call_count, 2)
            self.assertEqual(validate.call_count, 1)

    def test_shared_worker_checks_are_serialized_and_keep_one_validation(self):
        with self.journal.shared_locked(), patch("uqmc_recovery.run_authority.validate_section_authority",
                wraps=validate_section_authority) as validate:
            cache = self.cache()
            def worker(index):
                self.journal.append("DESIRED_READ", payload={"source_id": "source", "index": index})
                return cache.check()["proposal_hash"]
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(worker, range(12))), [self.plan_hash] * 12)
            self.assertEqual(validate.call_count, 1)

    def test_reported_workbook_edit_blocks_dispatch_after_cached_validation(self):
        with self.journal.locked():
            cache = self.cache()
            self.journal.append("BATCH_REFRESH_REQUIRED", payload={"proposal_hash": self.plan_hash})
            with self.assertRaisesRegex(SafetyError, "PLAN_EDITS_REQUIRE_NEW_REFRESHED_BATCH"):
                cache.check()

    def test_section_revocation_after_cache_blocks_and_poisons_instance(self):
        with self.journal.locked():
            cache = self.cache()
            self.journal.append("SECTION_APPROVAL_REVOKED", payload={"approval_id": self.fixture.section_approval_id})
            for _ in range(2):
                with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_REVOKED_OR_SUPERSEDED"):
                    cache.check()

    def test_section_supersession_after_cache_blocks(self):
        with self.journal.locked():
            cache = self.cache()
            self.journal.append("SECTION_APPROVAL_SUPERSEDED", payload={"approval_id": self.fixture.section_approval_id})
            with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_REVOKED_OR_SUPERSEDED"):
                cache.check()

    def test_handoff_revocation_blocks_on_initial_validation_and_tail(self):
        with self.journal.locked():
            cache = self.cache()
            self.journal.append("REVOKED", payload={"approval_id": self.fixture.approval.approval_id})
            for action in (cache.check, self.cache):
                with self.assertRaisesRegex(SafetyError, "APPROVAL_REVOKED"):
                    action()

    def test_section_expiry_checked_without_any_new_event(self):
        with self.journal.locked():
            cache = self.cache()
            with patch("uqmc_recovery.run_authority.datetime", wraps=datetime) as clock:
                clock.now.return_value = self.section_expiry
                with self.assertRaisesRegex(SafetyError, "SECTION_APPROVAL_EXPIRED"):
                    cache.check()

    def test_handoff_expiry_checked_without_any_new_event(self):
        with self.journal.locked():
            cache = self.cache()
            with patch("uqmc_recovery.run_authority.datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime.now(timezone.utc) + timedelta(minutes=90)
                with self.assertRaisesRegex(SafetyError, "APPROVAL_EXPIRED_OR_FUTURE"):
                    cache.check()

    def test_relevant_source_parent_descendant_and_proposal_exclusions_block(self):
        # Each case has its own run; an exclusion is deliberately never undone.
        for kind, payload in (("EXCLUDED", {"file_id": "target-section"}),
                              ("EXCLUDED", {"file_id": "existing"}),
                              ("EXCLUDED", {"file_id": "source"}),
                              ("PROPOSAL_EXCLUDED", {"proposal_ref": "ROOT/ROOT-ITEM"})):
            with self.subTest(kind=kind, payload=payload), self.journal.locked():
                # The prior relevant exclusion already blocks a new full cache,
                # so test each scope on a fresh fixture authority store instead.
                fixture = RunAuthorityTests("test_full_validation_once_then_tail_and_stats_only")
                fixture.setUp()
                try:
                    with fixture.journal.locked():
                        cache = fixture.cache()
                        fixture.journal.append(kind, payload=payload)
                        with self.assertRaisesRegex(SafetyError, kind):
                            cache.check()
                finally:
                    fixture.doCleanups()

    def test_unrelated_changes_do_not_reauthorize_or_block(self):
        with self.journal.locked():
            cache = self.cache()
            for kind, payload in (("EXCLUDED", {"file_id": "other-source"}),
                                  ("PROPOSAL_EXCLUDED", {"proposal_ref": "OTHER/ITEM"}),
                                  ("REVOKED", {"approval_id": "other-approval"}),
                                  ("APPROVED", {"proposal_hash": "other-hash", "run_id": "other-run"}),
                                  ("DESIRED_APPLIED", {"source_id": "source", "proposal_hash": self.plan_hash})):
                self.journal.append(kind, payload=payload)
            self.assertEqual(cache.check()["proposal_hash"], self.plan_hash)

    def test_new_authorization_invalidates_cache_even_for_identical_hash(self):
        with self.journal.locked():
            cache = self.cache()
            self.journal.append("HANDOFF_AUTHORIZED", payload=self.auth_event["payload"])
            with self.assertRaisesRegex(SafetyError, "BINDING_CHANGED_REINITIALIZE"):
                cache.check()

    def test_new_approval_for_same_batch_invalidates_cache(self):
        with self.journal.locked():
            cache = self.cache()
            self.journal.append("APPROVED", payload={"batch_id": self.plan.batch_id, "proposal_hash": "changed-hash"})
            with self.assertRaisesRegex(SafetyError, "BINDING_CHANGED_REINITIALIZE"):
                cache.check()

    def test_new_section_origin_with_new_batch_and_hash_invalidates_cache(self):
        with self.journal.locked():
            cache = self.cache()
            ref = self.journal.evidence({"projection": {"section_id": "fixture-section"}})
            self.journal.append("SECTION_IMPORTED", payload={"proposal_hash": "changed-hash",
                "batch_id": "new-batch", "run_id": "new-run", "manifest_ref": ref})
            with self.assertRaisesRegex(SafetyError, "BINDING_CHANGED_REINITIALIZE"):
                cache.check()

    def test_modified_evidence_detected_even_if_size_and_mtime_restored(self):
        with self.journal.locked():
            cache = self.cache()
            original = self.auth_path.read_bytes()
            before = self.auth_path.stat()
            self.auth_path.write_bytes(original.replace(b'"authorized":true', b'"authorized":null'))
            os.utime(self.auth_path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(self.auth_path.stat().st_size, before.st_size)
            with self.assertRaisesRegex(SafetyError, "RUN_AUTHORITY_EVIDENCE_CHANGED"):
                cache.check()

    def test_same_bytes_evidence_replacement_is_detected(self):
        with self.journal.locked():
            cache = self.cache()
            replacement = self.auth_path.with_suffix(".replacement")
            replacement.write_bytes(self.auth_path.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(self.auth_path)
            with self.assertRaisesRegex(SafetyError, "RUN_AUTHORITY_EVIDENCE_CHANGED"):
                cache.check()

    def test_missing_evidence_is_detected(self):
        with self.journal.locked():
            cache = self.cache()
            self.auth_path.unlink()
            with self.assertRaisesRegex(SafetyError, "RUN_AUTHORITY_EVIDENCE_CHANGED"):
                cache.check()

    def test_unchanged_normal_evidence_reads_do_not_change_fingerprint(self):
        with self.journal.locked():
            cache = self.cache()
            self.journal.read_evidence(self.auth_ref)
            self.assertTrue(cache.check()["authorized"])

    def test_cache_cannot_outlive_controller_lock_and_restart_revalidates(self):
        with patch("uqmc_recovery.run_authority.validate_section_authority", wraps=validate_section_authority) as validate:
            with self.journal.locked():
                old = self.cache()
                old.check()
            with self.journal.locked():
                with self.assertRaisesRegex(SafetyError, "LOCK_LIFETIME_ENDED"):
                    old.check()
                self.assertTrue(self.cache().check()["authorized"])
            self.assertEqual(validate.call_count, 2)

    def test_constructor_requires_outer_lock_and_exact_plan_hash(self):
        with self.assertRaisesRegex(SafetyError, "LOCK_LIFETIME_ENDED"):
            self.cache()
        with self.journal.locked(), self.assertRaisesRegex(SafetyError, "PLAN_HASH_MISMATCH"):
            RunAuthority(self.journal, self.plan, "incorrect-plan-hash", False)

    def test_skip_permission_audit_still_requires_accepted_exposure(self):
        original = self.journal.read_evidence
        def unaccepted(reference):
            value = original(reference)
            return value | {"access_policy": "PRESERVE_ACCESS"} if reference == self.auth_ref else value
        with self.journal.locked(), patch.object(self.journal, "read_evidence", side_effect=unaccepted):
            with self.assertRaisesRegex(SafetyError, "SKIP_PERMISSION_AUDIT_REQUIRES_ACCEPTED_SECTION_EXPOSURE"):
                self.cache()


if __name__ == "__main__":
    unittest.main()
