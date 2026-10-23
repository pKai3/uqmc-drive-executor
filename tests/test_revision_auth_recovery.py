"""Authentication expiry and revision diagnostics without real credentials/network."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from test_desired_transport import Service
from uqmc_recovery.direct_transport import GoogleDriveTransport, TransportError, METADATA_SCOPE
from uqmc_recovery.destination import RevisionGuard


class RevisionAuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.sleep=patch('uqmc_recovery.direct_transport.time.sleep').start()
        self.log=patch('uqmc_recovery.direct_transport._LOG.warning').start()
        self.addCleanup(patch.stopall)

    def test_refresh_timeout_then_success_happens_before_one_metadata_request(self):
        service=Service({'id':'file-a'})
        refresh=Mock(side_effect=[TimeoutError('private-token-value'),None])
        t=GoogleDriveTransport(service,allowed_ids={'file-a'},refresh_credentials=refresh)
        self.assertEqual(t.get_file('file-a')['id'],'file-a')
        self.assertEqual(refresh.call_count,2);self.assertEqual(len(service.executions),1)
        self.assertNotIn('private-token-value',str(self.log.call_args_list))

    def test_permanent_grant_failure_is_terminal_and_sanitized(self):
        from google.auth.exceptions import RefreshError
        refresh=Mock(side_effect=RefreshError('private-token-value',{'error':'invalid_grant','error_description':'private-token-value'}))
        service=Service();t=GoogleDriveTransport(service,allowed_ids={'file-a'},refresh_credentials=refresh)
        with self.assertRaises(TransportError) as caught:t.get_file('file-a')
        self.assertEqual(refresh.call_count,1);self.assertFalse(service.executions)
        detail=caught.exception.as_dict()
        self.assertEqual(detail['provider_error']['oauth_error'],'invalid_grant')
        self.assertNotIn('private-token-value',json.dumps(detail));self.sleep.assert_not_called()

    def test_expiry_uses_fresh_15_second_http_and_bounded_sdk_request_budget(self):
        import httplib2
        from google.oauth2.credentials import Credentials
        credentials=Credentials(token='private-old-token',refresh_token='private-refresh-token',
            client_id='private-client',client_secret='private-secret',token_uri='https://oauth2.googleapis.com/token',
            scopes=[METADATA_SCOPE],expiry=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=1))
        clients=[];calls=[]
        def network(http, uri, method='GET', **kw):
            clients.append(http);calls.append(method)
            if method=='POST':
                if calls.count('POST')==1:raise TimeoutError('private-token-value')
                return httplib2.Response({'status':'200'}),json.dumps({'access_token':'private-new-token','expires_in':3600,'token_type':'Bearer','scope':METADATA_SCOPE}).encode()
            return httplib2.Response({'status':'200'}),b'{"id":"file-a"}'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture.json';path.write_text('not credentials');path.chmod(0o600)
            with patch.object(Credentials,'from_authorized_user_file',return_value=credentials):
                t=GoogleDriveTransport.from_authorized_user_file(path,allowed_ids={'file-a'},audit_permissions=False)
            with patch.object(httplib2.Http,'request',autospec=True,side_effect=network):
                self.assertEqual(t.get_file('file-a')['id'],'file-a')
            self.assertEqual(path.read_text(),'not credentials')
        self.assertEqual(calls,['POST','POST','GET'])
        self.assertIsNot(clients[0],clients[1]);self.assertIsNot(clients[1],clients[2])
        self.assertTrue(all(http.timeout==15 for http in clients))
        self.assertTrue(credentials.valid)
        self.assertNotIn('private',str(self.log.call_args_list))

    def test_revision_guard_reports_safe_auth_context_and_stops_once(self):
        error=TransportError('AUTH_REQUEST_TIMEOUT',request='oauth.refresh',read_attempts=3)
        capture=SimpleNamespace(_revision=Mock(side_effect=error));reports=[]
        guard=RevisionGuard(capture,{'version':'1'},reports.append,interval=0)
        guard.runner=SimpleNamespace(stop_requested=False)
        guard({'event':'tick'});guard({'event':'tick'})
        self.assertTrue(guard.runner.stop_requested);self.assertEqual(capture._revision.call_count,1)
        stopped=[r for r in reports if r.get('stage')=='scheduling_stopped']
        self.assertEqual(len(stopped),1);self.assertEqual(stopped[0]['request_error'],error.as_dict())

    def test_revision_guard_does_not_print_arbitrary_exception_text(self):
        reports=[];guard=RevisionGuard(SimpleNamespace(_revision=Mock(side_effect=ValueError('private-token-value'))),{},reports.append,interval=0)
        guard.runner=SimpleNamespace(stop_requested=False);guard({'event':'tick'})
        self.assertNotIn('private-token-value',json.dumps(reports))
        self.assertEqual(guard.error['error_class'],'ValueError')

    def test_refresh_retryable_status_has_one_send_per_attempt_and_no_resource_write(self):
        import httplib2
        from google.oauth2.credentials import Credentials
        credentials=Credentials(token='private-old-token',refresh_token='private-refresh-token',
            client_id='private-client',client_secret='private-secret',token_uri='https://oauth2.googleapis.com/token',
            scopes=[METADATA_SCOPE],expiry=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=1))
        calls=[]
        def network(http,uri,method='GET',**kw):
            calls.append(method)
            return httplib2.Response({'status':'503'}),b'{"error":"server_error","error_description":"private-secret"}'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture.json';path.write_text('not credentials');path.chmod(0o600)
            with patch.object(Credentials,'from_authorized_user_file',return_value=credentials):
                t=GoogleDriveTransport.from_authorized_user_file(path,apply=True,allowed_ids={'file-a'},audit_permissions=False)
            with patch.object(httplib2.Http,'request',autospec=True,side_effect=network):
                with self.assertRaises(TransportError) as caught:t.update_metadata({'fileId':'file-a','name':'Approved'})
        self.assertEqual(calls,['POST']*3)
        self.assertEqual(caught.exception.status,503)
        self.assertEqual(caught.exception.request,'oauth.refresh')
        self.assertEqual(caught.exception.outcome,'NOT_DISPATCHED')
        self.assertEqual(caught.exception.read_attempts,3)
        self.assertNotIn('private-secret',json.dumps(caught.exception.as_dict()))
