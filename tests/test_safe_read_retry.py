"""Bounded read retries, page progress and write fencing; no live network."""
import http.client
import json
import types
import unittest
from unittest.mock import Mock, patch

from test_desired_transport import Request, Service
from uqmc_recovery.direct_transport import GoogleDriveTransport, TransportError
from uqmc_recovery.desired import DesiredRunner


def http_error(status, reason=None):
    exc=RuntimeError('private provider payload')
    exc.resp=types.SimpleNamespace(status=status)
    exc.content=json.dumps({'error':{'errors':[{'reason':reason}]}}).encode()
    return exc


class SafeReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.sleep=patch('uqmc_recovery.direct_transport.time.sleep').start()
        self.random=patch('uqmc_recovery.direct_transport.random.uniform',side_effect=lambda a,b:b).start()
        self.log=patch('uqmc_recovery.direct_transport._LOG.warning').start()
        self.addCleanup(patch.stopall)

    def transport(self,*values):
        service=Service(*values)
        return GoogleDriveTransport(service,allowed_ids={'file-a','parent-a'},
                                    apply=True,audit_permissions=False),service

    def test_timeout_then_connection_error_then_success_has_bounded_jitter(self):
        t,s=self.transport(TimeoutError('private'),ConnectionResetError(),{'id':'file-a'})
        self.assertEqual(t.get_file('file-a')['id'],'file-a')
        self.assertEqual(len(s.executions),3)
        self.assertEqual(self.random.call_args_list,[((1,2),),((2,4),)])
        self.assertEqual(self.sleep.call_args_list,[((2,),),((4,),)])
        self.assertEqual(self.log.call_count,2)
        self.assertNotIn('private',str(self.log.call_args_list))
        self.assertTrue(all(x['retries']==0 for x in s.executions))

    def test_retryable_http_and_transport_errors(self):
        errors=[http_error(s) for s in (408,429,500,502,503,504)]
        errors += [http_error(403,r) for r in ('rateLimitExceeded','userRateLimitExceeded')]
        errors += [http.client.BadStatusLine('private'),http.client.IncompleteRead(b'private')]
        for error in errors:
            with self.subTest(error=type(error),status=getattr(getattr(error,'resp',None),'status',None)):
                t,s=self.transport(error,{'id':'file-a'})
                self.assertEqual(t.get_file('file-a')['id'],'file-a')
                self.assertEqual(len(s.executions),2)

    def test_permanent_http_and_non_network_failures_are_terminal(self):
        errors=[http_error(s) for s in (400,401,404,409,412)]
        errors += [http_error(403,r) for r in ('insufficientFilePermissions','forbidden','dailyLimitExceeded')]
        errors += [ValueError('private'),PermissionError('private')]
        for error in errors:
            with self.subTest(error=type(error)):
                t,s=self.transport(error,{'id':'file-a'})
                with self.assertRaises(TransportError) as caught:t.get_file('file-a')
                self.assertEqual(caught.exception.read_attempts,1)
                self.assertEqual(len(s.executions),1)
        self.sleep.assert_not_called()

    def test_exhaustion_keeps_request_context_and_budget(self):
        t,s=self.transport(*[TimeoutError('private')] * 4)
        with self.assertRaises(TransportError) as caught:t.get_file('file-a')
        self.assertEqual(caught.exception.as_dict(),{'code':'DRIVE_REQUEST_TIMEOUT','status':None,
            'outcome':'READ_FAILED','request':'files.get','item_id':'file-a','read_attempts':3})
        self.assertEqual(len(s.executions),3);self.assertEqual(self.sleep.call_count,2)

    def test_pagination_retries_only_failed_page_without_duplicate_results(self):
        children=[{'id':'a','name':'A','parents':['parent-a']},{'id':'b','name':'B','parents':['parent-a']}]
        t,s=self.transport({'files':children[:1],'nextPageToken':'page2'},TimeoutError(),{'files':children[1:]})
        self.assertEqual(t.list_children('parent-a'),children)
        self.assertEqual([args['pageToken'] for _,args in s.requests],[None,'page2','page2'])
        self.assertTrue(all(method=='list' for method,_ in s.requests))

    def test_successful_read_does_not_inherit_failed_response_etag(self):
        t,s=self.transport(TimeoutError(),{'id':'file-a'})
        get=s.get
        def request(**args):
            s.response_headers={'etag':'"failed"'} if len(s.values)==2 else {}
            return get(**args)
        s.get=request
        self.assertNotIn('_http_etag',t.get_file('file-a'))

    def test_invalid_success_response_and_factory_error_are_not_retried(self):
        t,s=self.transport([])
        with self.assertRaisesRegex(TransportError,'INVALID_DRIVE_RESPONSE'):t.get_file('file-a')
        t,s=self.transport({'id':'different'})
        with self.assertRaisesRegex(TransportError,'DRIVE_RESPONSE_ID_MISMATCH'):t.get_file('file-a')
        s.get=Mock(side_effect=ValueError('bad fixture argument'))
        with self.assertRaises(ValueError):t.get_file('file-a')
        self.assertEqual(s.get.call_count,1);self.sleep.assert_not_called()

    def test_transient_auth_failure_retries_boundedly_before_read_loop(self):
        t,s=self.transport({'id':'file-a'})
        t._refresh_credentials=Mock(side_effect=TimeoutError())
        with self.assertRaisesRegex(TransportError,'AUTH_REQUEST_TIMEOUT'):t.get_file('file-a')
        self.assertEqual(t._refresh_credentials.call_count,3)
        self.assertEqual(s.requests,[]);self.assertEqual(self.sleep.call_count,2)

    def test_write_timeout_or_rate_limit_remains_one_dispatch(self):
        for error in (TimeoutError(),http_error(403,'rateLimitExceeded'),http_error(503)):
            t,s=self.transport(error,{'id':'file-a'})
            with self.assertRaises(TransportError) as caught:
                t.update_metadata({'fileId':'file-a','name':'Approved.pdf'})
            self.assertEqual(caught.exception.outcome,'UNKNOWN')
            self.assertIsNone(caught.exception.read_attempts)
            self.assertEqual(len(s.executions),1)
        self.sleep.assert_not_called()

    def test_id_allocation_and_sheets_capture_not_added_to_read_retry_policy(self):
        t,s=self.transport()
        for name in ('files.generateIds','spreadsheets.get'):
            factory=Mock(return_value=Request(s,TimeoutError()))
            with self.assertRaises(TransportError):t._read(factory,request=name)
            self.assertEqual(factory.call_count,1)
        self.sleep.assert_not_called()

    def test_runner_does_not_restart_transport_read_budget(self):
        t,s=self.transport(*[TimeoutError()] * 4)
        runner=types.SimpleNamespace(max_attempts=3,transient=DesiredRunner.transient,backoff=Mock())
        with self.assertRaises(TransportError):
            DesiredRunner.read_retry(runner,lambda:t.get_file('file-a'))
        self.assertEqual(len(s.executions),3);runner.backoff.assert_not_called()

if __name__=='__main__':unittest.main()
