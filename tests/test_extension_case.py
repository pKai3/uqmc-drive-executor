"""Case-only uploaded suffix changes preserve format and exact rollback names."""
import tempfile
import unittest
from pathlib import Path

from uqmc_recovery.journal import Journal
from uqmc_recovery.model import (Operation, SafetyError, validate_rename_name,
                                 validate_extension_evidence)


class ExtensionCaseTests(unittest.TestCase):
    def operation(self, old, new, mime="image/jpeg", correction=None, **kwargs):
        before = {"name": old, "mime_type": mime}
        return Operation("fixture-op", "fixture-id", "RENAME", before,
                         {"name": new}, extension_correction=correction or {}, **kwargs)

    def test_equivalent_casing_preserves_exact_target_and_preimage(self):
        cases = [("original.JPG", "Target.jpg", "image/jpeg"),
                 ("original.png", "Target.PNG", "image/png"),
                 ("original.JpEg", "Target.jPeG", "image/jpeg"),
                 ("original.PDF", "Target.pdf", "application/pdf"),
                 ("original.MOV", "Target.mov", "video/quicktime"),
                 ("original.TAR.GZ", "Target.tar.gz", "application/gzip"),
                 ("original.xyz", "Target.XYZ", "application/octet-stream")]
        for old, new, mime in cases:
            with self.subTest(old=old, new=new):
                op = self.operation(old, new, mime)
                validate_rename_name(op, op.before)
                self.assertEqual(op.before["name"], old)
                self.assertEqual(op.intended()["name"], new)

    def test_thirteen_saved_patterns_with_existing_correction_evidence(self):
        # Synthetic identities/names reproduce the saved twelve JPEG and one PNG
        # imports. Their real metadata and inventory remain outside version control.
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root) / "state")
            source_ref = journal.evidence({"kind": "fixture-metadata"})
            cases = [(f"Image {i}.JPG", f"Approved image {i}.jpg", "image/jpeg")
                     for i in range(12)] + [("Artwork.PNG", "Approved artwork.png", "image/png")]
            for old, new, mime in cases:
                with self.subTest(old=old):
                    proof = journal.evidence({"kind": "VERIFIED_EXTENSION_CORRECTION",
                        "source_id": "fixture-id", "source_exact_name": old,
                        "target_exact_name": new, "mime_type": mime,
                        "provenance_ref": source_ref})
                    correction = {"source_suffix": Path(old).suffix,
                        "target_suffix": Path(new).suffix, "mime_type": mime,
                        "evidence_ref": proof}
                    op = self.operation(old, new, mime, correction)
                    validate_rename_name(op, op.before)
                    validate_extension_evidence(journal, op)

    def test_real_extension_changes_and_removal_stay_blocked(self):
        for old, new, mime in [("original.JPG", "Target.png", "image/jpeg"),
                               ("original.PDF", "Target.docx", "application/pdf"),
                               ("original.JPG", "Target", "image/jpeg"),
                               ("original.tar.gz", "Target.GZ", "application/gzip")]:
            with self.subTest(old=old, new=new):
                correction = {"source_suffix": Path(old).suffix,
                    "target_suffix": Path(new).suffix, "mime_type": mime,
                    "evidence_ref": "fixture-proof"}
                op = self.operation(old, new, mime, correction)
                with self.assertRaisesRegex(SafetyError, "UPLOADED_EXTENSION_CHANGED"):
                    validate_rename_name(op, op.before)

    def test_existing_correction_proof_is_still_validated(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root) / "state")
            proof = journal.evidence({"kind": "VERIFIED_EXTENSION_CORRECTION",
                "source_id": "different-id", "source_exact_name": "original.JPG",
                "target_exact_name": "Target.jpg", "mime_type": "image/jpeg",
                "provenance_ref": journal.evidence({"kind": "fixture"})})
            op = self.operation("original.JPG", "Target.jpg", correction={
                "source_suffix": ".JPG", "target_suffix": ".jpg",
                "mime_type": "image/jpeg", "evidence_ref": proof})
            validate_rename_name(op, op.before)
            with self.assertRaisesRegex(SafetyError, "EXTENSION_CORRECTION_EVIDENCE_MISMATCH"):
                validate_extension_evidence(journal, op)

    def test_native_synthetic_suffix_remains_blocked(self):
        op = self.operation("Document", "Document.GDOC", "application/vnd.google-apps.document")
        with self.assertRaisesRegex(SafetyError, "SYNTHETIC_NATIVE_EXTENSION"):
            validate_rename_name(op, op.before)

    def test_rollback_restores_exact_historical_case(self):
        op = self.operation("Target.jpg", "original.JPG", direction="ROLLBACK",
                            original_operation_id="forward-op")
        validate_rename_name(op, op.before)
        self.assertEqual(op.intended()["name"], "original.JPG")


if __name__ == "__main__":
    unittest.main()
