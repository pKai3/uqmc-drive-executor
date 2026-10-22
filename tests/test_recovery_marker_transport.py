"""Drive field masks must retain creation markers needed for folder recovery."""
import copy
import unittest

from uqmc_recovery.direct_transport import GoogleDriveTransport
from uqmc_recovery.model import FOLDER


def top_level_fields(mask):
    fields, start, depth = [], 0, 0
    for index, character in enumerate(mask):
        if character == '(':
            depth += 1
        elif character == ')':
            depth -= 1
        elif character == ',' and depth == 0:
            fields.append(mask[start:index].split('(', 1)[0])
            start = index + 1
    fields.append(mask[start:].split('(', 1)[0])
    return fields


class FieldFilteredService:
    """Return only requested resource fields, like the real files.get API."""
    def __init__(self, properties=None):
        self.raw = {'id': 'fixture-created-folder', 'name': '2019',
            'parents': ['fixture-parent'], 'mimeType': FOLDER, 'trashed': False,
            'version': '2', 'modifiedTime': '2026-01-01T00:00:00Z',
            'owners': [{'permissionId': 'fixture-owner'}],
            'capabilities': {'canRename': True, 'canAddChildren': True,
                             'canMoveItemWithinDrive': True, 'canMoveItemOutOfDrive': True}}
        if properties is not None:
            self.raw['appProperties'] = properties
        self.requests, self.executions, self.permission_calls = [], [], 0

    def files(self):
        service = self
        class Files:
            def get(self, **parameters):
                service.requests.append(copy.deepcopy(parameters))
                requested = set(top_level_fields(parameters['fields']))
                class Request:
                    def execute(self, *, num_retries):
                        service.executions.append(num_retries)
                        return copy.deepcopy({k: v for k, v in service.raw.items() if k in requested})
                return Request()
        return Files()

    def permissions(self):
        self.permission_calls += 1
        raise AssertionError('Recovery marker reads must not call permissions.list')


class RecoveryMarkerTransportTests(unittest.TestCase):
    def transport(self, service, *, audit, apply=False):
        return GoogleDriveTransport(service, audit_permissions=audit, apply=apply,
            allowed_ids={'fixture-parent', 'fixture-created-folder'},
            creation_parent_ids={'fixture-parent'} if apply else ())

    def assert_single_marker_request(self, service):
        self.assertEqual(len(service.requests), 1)
        request = service.requests[0]
        self.assertEqual(request['fileId'], 'fixture-created-folder')
        self.assertIs(request['supportsAllDrives'], True)
        self.assertEqual(top_level_fields(request['fields']).count('appProperties'), 1)
        self.assertEqual(service.executions, [0])
        self.assertEqual(service.permission_calls, 0)

    def test_ordinary_reads_preserve_requested_marker_in_both_audit_modes(self):
        for audit in (False, True):
            with self.subTest(audit=audit):
                service = FieldFilteredService({'uqmcFolderKey': 'a' * 64})
                value = self.transport(service, audit=audit).get_file('fixture-created-folder')
                self.assertEqual(value['id'], 'fixture-created-folder')
                self.assertEqual(value.get('appProperties'), {'uqmcFolderKey': 'a' * 64})
                self.assertIs(value.get('_app_properties_requested'), True)
                self.assert_single_marker_request(service)
                if not audit:
                    self.assertNotIn('permissions', top_level_fields(service.requests[0]['fields']))

    def test_confirmed_absent_properties_stay_absent_with_request_evidence(self):
        for audit in (False, True):
            with self.subTest(audit=audit):
                service = FieldFilteredService()
                value = self.transport(service, audit=audit).get_file('fixture-created-folder')
                self.assertNotIn('appProperties', value)
                self.assertIs(value.get('_app_properties_requested'), True)
                self.assert_single_marker_request(service)

    def test_present_properties_without_creation_marker_are_not_invented(self):
        for properties in ({}, {'otherApplicationField': 'fixture'}):
            with self.subTest(properties=properties):
                service = FieldFilteredService(properties)
                value = self.transport(service, audit=False).get_file('fixture-created-folder')
                self.assertEqual(value.get('appProperties'), properties)
                self.assertNotIn('uqmcFolderKey', value.get('appProperties', {}))
                self.assertIs(value.get('_app_properties_requested'), True)
                self.assert_single_marker_request(service)

    def test_reserved_folder_read_requests_marker_once_in_both_audit_modes(self):
        for audit in (False, True):
            with self.subTest(audit=audit):
                service = FieldFilteredService({'uqmcFolderKey': 'b' * 64})
                transport = self.transport(service, audit=audit, apply=True)
                transport.register_reserved_folder_id('fixture-created-folder', 'fixture-parent')
                value = transport.get_reserved_folder('fixture-created-folder')
                self.assertEqual(value['id'], 'fixture-created-folder')
                self.assertEqual(value['appProperties'], {'uqmcFolderKey': 'b' * 64})
                self.assert_single_marker_request(service)


if __name__ == '__main__':
    unittest.main()
