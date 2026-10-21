"""Exact title policies preserve bytes/format and permit verified historical undo."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from uqmc_recovery.adapters import MockAdapter, fixture_state
from uqmc_recovery.engine import Engine
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, Operation, Plan, SafetyError, uploaded_extension
from test_engine import approval


class NamePolicyTests(unittest.TestCase):
    def roundtrip(self, old, new, mime="application/pdf", correction=False):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "state")
            before = fixture_state("fixture-file", old, mime)
            policy = {}
            if correction:
                provenance = journal.evidence({"kind": "FIXTURE_MIME_AND_EXACT_NAME_REVIEW"})
                proof = journal.evidence({"kind": "VERIFIED_EXTENSION_CORRECTION", "source_id": before["id"],
                    "source_exact_name": old, "target_exact_name": new, "mime_type": mime,
                    "provenance_ref": provenance})
                policy = {"source_suffix": uploaded_extension(old), "target_suffix": uploaded_extension(new),
                          "mime_type": mime, "evidence_ref": proof}
            op = Operation("fixture-rename", before["id"], "RENAME", before, {"name": new},
                           extension_correction=policy)
            plan = Plan("fixture-names", "fixture-names", "reviewed-names", (op,))
            adapter = MockAdapter({before["id"]: before, "origin": fixture_state("origin", "Original", FOLDER, ())})
            engine = Engine(journal, adapter)
            self.assertEqual(engine.run(plan, approval(plan))[op.operation_id], "VERIFIED_APPLIED")
            inverse = engine.rollback_plan(plan, [op.operation_id], "undo-names")
            self.assertEqual(set(engine.run(inverse, approval(inverse)).values()), {"VERIFIED_APPLIED"})
            self.assertEqual(adapter.states[before["id"]]["name"], old)
            self.assertEqual(adapter.states[before["id"]]["mime_type"], mime)
            self.assertEqual(len(adapter.calls), 2)

    def test_dotted_stem_is_not_an_immutable_extension(self):
        self.roundtrip("Fixture_FA.CPR.pdf", "2025.01.02 - Fixture CPR.pdf")

    def test_date_stem_forward_and_inverse(self):
        self.roundtrip("Fixture 2024.pdf", "2024.03.02 - Certificate.pdf")

    def test_missing_historical_extension_restored_exactly(self):
        self.roundtrip("Fixture FA CPR", "2024.03.02 - Certificate.pdf")

    def test_wrong_cpr_suffix_corrected_using_verified_pdf_mime(self):
        self.roundtrip("Fixture_FA.CPR", "2024.03.02 - Certificate.pdf", correction=True)

    def test_mislabeled_png_corrected_then_historical_pdf_title_restored(self):
        self.roundtrip("Fixture FA.pdf", "2024.03.02 - Certificate.png", "image/png", correction=True)

    def test_mime_correction_without_explicit_evidence_rejected(self):
        op = Operation("op", "fixture-file", "RENAME", fixture_state("fixture-file", "Wrong.pdf", "image/png"),
                       {"name": "Correct.png"})
        with self.assertRaisesRegex(SafetyError, "UPLOADED_EXTENSION_CHANGED"):
            op.validate()

    def test_native_display_extension_still_rejected(self):
        op = Operation("op", "fixture-file", "RENAME", fixture_state("fixture-file", "Document", "application/vnd.google-apps.document"),
                       {"name": "Document.gdoc"})
        with self.assertRaisesRegex(SafetyError, "SYNTHETIC_NATIVE_EXTENSION"):
            op.validate()

    def test_fabricated_rollback_cannot_bypass_forward_name_policy(self):
        before = fixture_state("fixture-file", "Current.pdf")
        op = Operation("undo", "fixture-file", "RENAME", before, {"name": "Arbitrary.CPR"},
                       direction="ROLLBACK", original_operation_id="nonexistent")
        plan = Plan("undo-batch", "undo-batch", "undo-revision", (op,))
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(Journal(Path(tmp) / "state"), MockAdapter({}))
            with self.assertRaisesRegex(SafetyError, "ROLLBACK_ORIGINAL_NOT_VERIFIED"):
                engine.register(plan)


if __name__ == "__main__":
    unittest.main()
