import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from uqmc_recovery.direct_transport import GoogleDriveTransport, TransportError
from uqmc_recovery.folder_creation import ensure_required_folder
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, SafetyError


class FakeDrive:
    def __init__(self):
        self.parent = {"id": "parent", "name": "Approved", "mimeType": FOLDER,
                       "parents": ["ancestor"], "trashed": False,
                       "capabilities": {"canAddChildren": True}}
        self.files, self.calls = {}, []
        self.failure, self.counter = None, 0

    def get_file(self, file_id):
        self.calls.append(("get", file_id))
        if file_id == "parent":
            return copy.deepcopy(self.parent)
        return copy.deepcopy(self.files[file_id])

    def list_children(self, parent):
        self.calls.append(("list", parent))
        return [copy.deepcopy(x) for x in self.files.values() if x["parents"] == [parent]]

    def reserve_folder_id(self, parent):
        self.counter += 1
        self.calls.append(("allocate", parent))
        return "allocated-" + str(self.counter)

    def register_reserved_folder_id(self, file_id, parent):
        self.calls.append(("register", file_id))

    def get_reserved_folder(self, file_id):
        self.calls.append(("read", file_id))
        if file_id not in self.files:
            raise TransportError("DRIVE_HTTP_ERROR", status=404)
        return copy.deepcopy(self.files[file_id])

    def create_reserved_folder(self, file_id, *, name, parent_id, stable_key):
        self.calls.append(("create", file_id))
        if self.failure == "refuse":
            raise TransportError("DRIVE_HTTP_ERROR", status=403)
        if self.failure == "before":
            raise TransportError("DRIVE_REQUEST_TIMEOUT", outcome="UNKNOWN")
        self.files[file_id] = {"id": file_id, "name": name, "mimeType": FOLDER,
                              "parents": [parent_id], "trashed": False,
                              "capabilities": {"canAddChildren": True},
                              "appProperties": {"uqmcFolderKey": stable_key}}
        if self.failure == "after":
            raise TransportError("DRIVE_REQUEST_TIMEOUT", outcome="UNKNOWN")
        return copy.deepcopy(self.files[file_id])


class FolderCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.j = Journal(self.temp.name)
        self.drive = FakeDrive()
        provenance = self.j.evidence({"kind": "TRUSTED_USER_INSTRUCTION", "text": "Complete this section."})
        self.authority = {"schema_version": "uqmc.folder-create-authority.v1", "section_id": "section",
            "approval_id": "approval", "decision": "APPROVED", "provenance_ref": provenance,
            "access_policy": "SCOPED_INHERITANCE_ACCEPTED", "folders": [
                {"key": "section", "name": "Section", "parent_id": "parent"},
                {"key": "section/year", "name": "Year", "parent_key": "section"}]}
        self.ref = self.j.evidence(self.authority)

    def run_folder(self, **kwargs):
        return ensure_required_folder(self.j, self.drive, key="section", name="Section",
                                      parent_id="parent", authority_ref=self.ref, **kwargs)

    def test_create_wal_precedes_write_and_resume_does_not_create_again(self):
        original = self.drive.create_reserved_folder
        def create(*args, **kwargs):
            kinds = [x["kind"] for x in self.j.records()]
            self.assertEqual(kinds[-2:], ["REQUIRED_FOLDER_PREPARED", "REQUIRED_FOLDER_ATTEMPTED"])
            return original(*args, **kwargs)
        self.drive.create_reserved_folder = create
        first = self.run_folder()
        second = self.run_folder()
        self.assertEqual(first, second)
        self.assertEqual([c for c in self.drive.calls if c[0] == "create"], [("create", "allocated-1")])
        self.assertEqual(self.drive.counter, 1)

    def test_lost_success_response_reconciles(self):
        self.drive.failure = "after"
        value = self.run_folder()
        self.assertEqual(value["id"], "allocated-1")
        self.assertTrue(self.j.records()[-1]["payload"]["reconciled"])
        self.assertEqual(len([c for c in self.drive.calls if c[0] == "create"]), 1)

    def test_transient_failure_resumes_same_id(self):
        self.drive.failure = "before"
        with self.assertRaises(TransportError):
            self.run_folder()
        self.drive.failure = None
        self.assertEqual(self.run_folder()["id"], "allocated-1")
        self.assertEqual(self.drive.counter, 1)
        self.assertEqual([c[1] for c in self.drive.calls if c[0] == "create"], ["allocated-1", "allocated-1"])

    def test_refusal_is_not_automatically_retried(self):
        self.drive.failure = "refuse"
        with self.assertRaises(TransportError):
            self.run_folder()
        with self.assertRaisesRegex(SafetyError, "PRIOR_REFUSAL"):
            self.run_folder()
        self.assertEqual(len([c for c in self.drive.calls if c[0] == "create"]), 1)

    def test_crash_after_remote_success_before_receipt_resumes_by_id(self):
        append = self.j.append
        def interrupted(kind, *args, **kwargs):
            if kind == "REQUIRED_FOLDER_RECEIPT":
                raise KeyboardInterrupt()
            return append(kind, *args, **kwargs)
        with patch.object(self.j, "append", interrupted), self.assertRaises(KeyboardInterrupt):
            self.run_folder()
        value = self.run_folder()
        self.assertEqual(value["id"], "allocated-1")
        self.assertEqual(len([c for c in self.drive.calls if c[0] == "create"]), 1)

    def test_crash_at_each_transition_does_not_duplicate(self):
        transitions = ["REQUIRED_FOLDER_ALLOCATED", "REQUIRED_FOLDER_PREPARED", "REQUIRED_FOLDER_ATTEMPTED",
                       "REQUIRED_FOLDER_RECEIPT", "REQUIRED_FOLDER_VERIFIED"]
        for kind in transitions:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as path:
                j, drive = Journal(path), FakeDrive()
                proof = j.evidence(self.j.read_evidence(self.authority["provenance_ref"]))
                ref = j.evidence({**self.authority, "provenance_ref": proof})
                append = j.append
                def fail(current, *args, **kwargs):
                    result = append(current, *args, **kwargs)
                    if current == kind:
                        raise KeyboardInterrupt()
                    return result
                args = dict(key="section", name="Section", parent_id="parent", authority_ref=ref)
                with patch.object(j, "append", fail), self.assertRaises(KeyboardInterrupt):
                    ensure_required_folder(j, drive, **args)
                value = ensure_required_folder(j, drive, **args)
                self.assertEqual(value["id"], "allocated-1")
                self.assertEqual(len([c for c in drive.calls if c[0] == "create"]), 1)

    def test_nested_parent_dependency(self):
        with self.assertRaisesRegex(SafetyError, "DEPENDENCY_UNVERIFIED"):
            ensure_required_folder(self.j, self.drive, key="section/year", name="Year", parent_id="allocated-1", authority_ref=self.ref)
        root = self.run_folder()
        child = ensure_required_folder(self.j, self.drive, key="section/year", name="Year", parent_id=root["id"], authority_ref=self.ref)
        self.assertEqual(child["parents"], [root["id"]])

    def test_mismatched_authority_never_calls_drive(self):
        with self.assertRaisesRegex(SafetyError, "OUTSIDE_APPROVED_SCOPE"):
            ensure_required_folder(self.j, self.drive, key="section", name="Other", parent_id="parent", authority_ref=self.ref)
        self.assertEqual(self.drive.calls, [])

    def test_revocation_never_calls_drive(self):
        self.j.append("SECTION_APPROVAL_REVOKED", payload={"approval_id": "approval"})
        with self.assertRaisesRegex(SafetyError, "AUTHORITY_REVOKED"):
            self.run_folder()
        self.assertEqual(self.drive.calls, [])

    def test_same_name_is_not_adopted(self):
        self.drive.files["other"] = {"id": "other", "name": "Section", "parents": ["parent"]}
        with self.assertRaisesRegex(SafetyError, "NAME_COLLISION"):
            self.run_folder()
        self.assertFalse(any(c[0] == "create" for c in self.drive.calls))

    def test_created_folder_changed_by_human_blocks(self):
        self.run_folder()
        self.drive.files["allocated-1"]["name"] = "Human change"
        with self.assertRaisesRegex(SafetyError, "STATE_CONFLICT"):
            self.run_folder()
        self.assertEqual(len([c for c in self.drive.calls if c[0] == "create"]), 1)

    def test_parent_changes_between_reads_block(self):
        original = self.drive.get_file
        count = [0]
        def read(file_id):
            count[0] += 1
            result = original(file_id)
            if count[0] == 2:
                result["name"] = "Changed"
            return result
        self.drive.get_file = read
        with self.assertRaisesRegex(SafetyError, "PARENT_DRIFT"):
            self.run_folder()
        self.assertFalse(any(c[0] == "create" for c in self.drive.calls))


class CreationTransportTests(unittest.TestCase):
    def test_exact_preallocated_body_and_zero_hidden_retries(self):
        from tests.test_direct_transport import Service
        service = Service({"ids": ["generated"]}, {"id": "generated"})
        transport = GoogleDriveTransport(service, apply=True, allowed_ids={"parent"}, mutation_ids=(),
                                        creation_parent_ids={"parent"}, audit_permissions=False)
        allocated = transport.reserve_folder_id("parent")
        transport.create_reserved_folder(allocated, name="Folder", parent_id="parent", stable_key="a" * 64)
        calls = [c for c in service.calls if c[0] == "files.create"]
        self.assertEqual(len(calls), 1)
        args = calls[0][1]
        self.assertEqual(args["body"], {"id": "generated", "name": "Folder", "mimeType": FOLDER,
            "parents": ["parent"], "appProperties": {"uqmcFolderKey": "a" * 64}})
        self.assertTrue(args["ignoreDefaultVisibility"])
        self.assertFalse(any(c[0].startswith("permissions") for c in service.calls))
        self.assertTrue(all(c[1] == 0 for c in service.calls if c[0] == "execute"))
        with self.assertRaisesRegex(TransportError, "OUTSIDE_APPROVED_MUTATION_IDS"):
            transport.update_metadata({"fileId": allocated, "name": "Unapproved"})

    def test_creation_opt_in_and_reserved_id_required(self):
        from tests.test_direct_transport import Service
        service = Service()
        transport = GoogleDriveTransport(service, apply=True, allowed_ids={"parent"})
        with self.assertRaisesRegex(TransportError, "CREATION_PARENTS"):
            transport.reserve_folder_id("parent")
        transport.creation_parent_ids = {"parent"}
        with self.assertRaisesRegex(TransportError, "UNRESERVED_FOLDER_ID"):
            transport.create_reserved_folder("arbitrary", name="Folder", parent_id="parent", stable_key="a" * 64)
        self.assertEqual(service.calls, [])

    def test_factory_creation_requires_full_scope_and_rechecks_after_refresh(self):
        from google.oauth2.credentials import Credentials
        from uqmc_recovery.direct_transport import FULL_DRIVE_SCOPE, METADATA_SCOPE
        from tests.test_direct_transport import Service
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "synthetic-token.json"
            token.write_text("synthetic fixture only")
            token.chmod(0o600)
            for scope in (METADATA_SCOPE, FULL_DRIVE_SCOPE):
                credentials = Credentials(token="fixture", refresh_token="synthetic-refresh", scopes=[scope])
                service = Service({"ids": ["generated"]})
                with patch.object(Credentials, "from_authorized_user_file", return_value=credentials), patch("googleapiclient.discovery.build", return_value=service):
                    args = dict(apply=True, allowed_ids={"parent"}, creation_parent_ids={"parent"})
                    if scope == METADATA_SCOPE:
                        with self.assertRaisesRegex(TransportError, "FULL_DRIVE_SCOPE_REQUIRED"):
                            GoogleDriveTransport.from_authorized_user_file(token, **args)
                        self.assertEqual(service.calls, [])
                    else:
                        transport = GoogleDriveTransport.from_authorized_user_file(token, **args)
                        self.assertEqual(transport.reserve_folder_id("parent"), "generated")
                        self.assertEqual(service.calls[0], ("files.generateIds", {"count": 1, "space": "drive", "type": "files"}))
                        credentials.expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
                        def refresh(request):
                            credentials._granted_scopes = [METADATA_SCOPE]
                            credentials.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
                        with patch.object(credentials, "refresh", side_effect=refresh), self.assertRaisesRegex(TransportError, "FULL_DRIVE_SCOPE_REQUIRED"):
                            transport.create_reserved_folder("generated", name="Folder", parent_id="parent", stable_key="a" * 64)
                        self.assertFalse(any(c[0] == "files.create" for c in service.calls))
            self.assertEqual(token.read_text(), "synthetic fixture only")
