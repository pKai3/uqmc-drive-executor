import unittest
from uqmc_recovery.model import Operation, SafetyError, validate_rename_name

class DocxExtensionTests(unittest.TestCase):
    def test_verified_docx_suffix_for_versioned_extensionless_title(self):
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        before={"name":"V0.6 Yellow Tape","mime_type":mime}
        correction={"source_suffix":".6 Yellow Tape","target_suffix":".docx","mime_type":mime,"evidence_ref":"fixture-proof"}
        op=Operation("fixture-op","fixture-id","RENAME",before,{"name":"V0.6 Yellow Tape.docx"},extension_correction=correction)
        validate_rename_name(op,before)
        op.extension_correction.clear()
        with self.assertRaisesRegex(SafetyError,"UPLOADED_EXTENSION_CHANGED"):
            validate_rename_name(op,before)

    def test_docx_cannot_be_relabelled_pdf(self):
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        before={"name":"Original.docx","mime_type":mime}
        correction={"source_suffix":".docx","target_suffix":".pdf","mime_type":mime,"evidence_ref":"fixture-proof"}
        op=Operation("fixture-op","fixture-id","RENAME",before,{"name":"New.pdf"},extension_correction=correction)
        with self.assertRaisesRegex(SafetyError,"UPLOADED_EXTENSION_CHANGED"):
            validate_rename_name(op,before)
