"""Safe structured Google diagnostics; synthetic responses and temporary stores."""
import json
import traceback
import types
import unittest

import test_desired as desired_fixtures
from test_desired_transport import Service
from uqmc_recovery.direct_transport import GoogleDriveTransport, TransportError


def http_error(body, status=403):
    import httplib2
    from googleapiclient.errors import HttpError
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    try:
        return HttpError(httplib2.Response({'status': str(status),
            'authorization': 'Bearer header-only-secret'}), content,
            uri='https://example.invalid/private?access_token=uri-only-secret')
    except AttributeError:
        # Malformed message types can fail inside Google's constructor first;
        # still exercise the transport parser against an arbitrary exception.
        error = RuntimeError('malformed provider payload')
        error.resp = types.SimpleNamespace(status=status)
        error.content = content
        return error


def provider_body(message='The user does not have sufficient permissions for this file.',
                  reasons=('insufficientFilePermissions',)):
    return {'error': {'code': 403, 'message': message,
        'errors': [{'reason': reason, 'message': message} for reason in reasons]}}


class ProviderErrorTests(unittest.TestCase):
    def capture(self, body, *, write=True, secrets=()):
        service = Service(http_error(body))
        transport = GoogleDriveTransport(service, apply=True,
            allowed_ids={'file-a'}, mutation_ids={'file-a'}, audit_permissions=False,
            secret_values=lambda: secrets)
        try:
            if write:
                transport.update_metadata({'fileId': 'file-a', 'name': 'Approved.pdf'})
            else:
                transport.get_file('file-a')
        except TransportError as error:
            return error, service
        self.fail('Expected one sanitized transport error')

    def test_structured_reason_and_message_survive_without_uri_headers_or_traceback(self):
        message = 'The user does not have sufficient permissions for this file.'
        for write in (True, False):
            with self.subTest(write=write):
                error, service = self.capture(provider_body(message), write=write)
                self.assertEqual(error.as_dict()['provider_error'], {
                    'reasons': ['insufficientFilePermissions'], 'message': message})
                self.assertEqual(error.outcome, 'UNKNOWN' if write else 'READ_FAILED')
                self.assertFalse(error.transient)
                output = json.dumps(error.as_dict())
                trace = ''.join(traceback.format_exception(error))
                for private in ('header-only-secret', 'uri-only-secret', 'https://example.invalid'):
                    self.assertNotIn(private, output + trace)
                self.assertNotIn(message, str(error))
                self.assertNotIn(message, trace)
                self.assertEqual(len(service.executions), 1)
                self.assertEqual(service.executions[0]['retries'], 0)

    def test_credentials_and_urls_redacted_but_reason_retained(self):
        message = ('Permission denied. Authorization: Bearer raw-bearer-token; '
            'client_secret=raw-client-secret refresh_token=raw-refresh-token '
            'https://example.test/?access_token=raw-url-token '
            'known value short-secret')
        error, _ = self.capture(provider_body(message), secrets=('short-secret',))
        output = json.dumps(error.as_dict())
        for private in ('raw-bearer-token', 'raw-client-secret', 'raw-refresh-token',
                        'raw-url-token', 'https://example.test', 'short-secret'):
            self.assertNotIn(private, output)
        self.assertEqual(error.as_dict()['provider_error']['reasons'], ['insufficientFilePermissions'])

    def test_only_selected_structured_fields_are_captured(self):
        body = provider_body()
        body['token'] = 'top-level-extra-secret'
        body['error']['credentials'] = {'access_token': 'nested-extra-secret'}
        body['error']['errors'][0]['location'] = 'location-extra-secret'
        body['error']['errors'][0]['debugInfo'] = 'debug-extra-secret'
        error, _ = self.capture(body)
        self.assertEqual(set(error.as_dict()['provider_error']), {'reasons', 'message'})
        for private in ('top-level-extra-secret', 'nested-extra-secret',
                        'location-extra-secret', 'debug-extra-secret'):
            self.assertNotIn(private, json.dumps(error.as_dict()))

    def test_malformed_or_oversized_body_is_not_captured(self):
        for body in (b'not-json', b'[]', b'{"error":[]}', b'x' * 65537,
                     b'{"error":{"message":[],"errors":[{"reason":[]} ]}}'):
            with self.subTest(body=body[:50]):
                error, _ = self.capture(body)
                self.assertNotIn('provider_error', error.as_dict())
                self.assertFalse(error.transient)

    def test_message_size_is_bounded(self):
        error, _ = self.capture(provider_body('A' * 4096))
        self.assertLessEqual(len(error.as_dict()['provider_error']['message']), 1024)

    def test_capture_does_not_change_rate_limit_or_permission_classification(self):
        for reasons, transient in ((('rateLimitExceeded',), True),
                (('userRateLimitExceeded',), True),
                (('insufficientFilePermissions',), False),
                (('rateLimitExceeded', 'insufficientFilePermissions'), False)):
            with self.subTest(reasons=reasons):
                error, service = self.capture(provider_body(reasons=reasons))
                self.assertEqual(error.transient, transient)
                self.assertEqual(error.as_dict()['provider_error']['reasons'], list(reasons))
                self.assertEqual(len(service.executions), 1)

    def test_sanitized_details_are_durable_in_receipt_without_retry(self):
        error, _ = self.capture(provider_body('Permission denied for short-secret.'),
                                secrets=('short-secret',))
        fixture = desired_fixtures.DesiredStateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.freeze()
        def refused(args):
            raise error
        fixture.transport.before_update = refused
        result = fixture.run_plan(fixture.runner(audit_permissions=False, max_attempts=3))
        self.assertEqual(result['counts'], {'NOT_APPLIED': 1})
        self.assertEqual(len(fixture.transport.calls), 1)
        receipt = next(r for r in fixture.journal.records() if r['kind'] == 'DESIRED_RECEIPT')
        self.assertEqual(receipt['payload']['request_error'], error.as_dict())
        self.assertEqual(receipt['payload']['request_error']['provider_error']['reasons'],
                         ['insufficientFilePermissions'])
        self.assertNotIn('short-secret', fixture.journal.path.read_text())
        observed = [r for r in fixture.journal.records() if r['kind'] == 'DESIRED_OBSERVED'][-1]
        self.assertEqual(observed['payload']['state'], 'NOT_APPLIED')


if __name__ == '__main__':
    unittest.main()
