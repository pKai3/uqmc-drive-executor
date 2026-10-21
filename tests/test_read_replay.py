import unittest
from uqmc_recovery.direct_transport import _single_send_http_class, _ReplayBlocked

class Connection:
    def __init__(self): self.calls=[]
    def request(self,*args,**kwargs): self.calls.append((args,kwargs))

class RetryingHttp:
    def request(self,connection,requests): return self._conn_request(connection,requests)
    def _conn_request(self,connection,requests):
        for method,path in requests: connection.request(method,path)

class ReadReplayTests(unittest.TestCase):
    def test_safe_read_stale_connection_retry_is_bounded(self):
        for method in ('GET','HEAD'):
            with self.subTest(method=method):
                http=_single_send_http_class(RetryingHttp)();conn=Connection()
                http.request(conn,[(method,'/fixture')]*2)
                self.assertEqual(len(conn.calls),2)
                with self.assertRaises(_ReplayBlocked):http.request(conn,[(method,'/fixture')]*3)
                self.assertEqual(len(conn.calls),4)

    def test_write_replay_remains_blocked(self):
        for method in ('PATCH','POST','PUT','DELETE'):
            with self.subTest(method=method):
                http=_single_send_http_class(RetryingHttp)();conn=Connection()
                with self.assertRaises(_ReplayBlocked):http.request(conn,[(method,'/fixture')]*2)
                self.assertEqual(len(conn.calls),1)

    def test_retry_cannot_switch_request(self):
        for second in [('GET','/different'),('PATCH','/fixture'),('HEAD','/fixture')]:
            with self.subTest(second=second):
                http=_single_send_http_class(RetryingHttp)();conn=Connection()
                with self.assertRaises(_ReplayBlocked):http.request(conn,[('GET','/fixture'),second])
                self.assertEqual(len(conn.calls),1)

    def test_separate_outer_writes_are_not_replays(self):
        http=_single_send_http_class(RetryingHttp)();conn=Connection()
        http.request(conn,[('PATCH','/fixture')]);http.request(conn,[('PATCH','/fixture')])
        self.assertEqual(len(conn.calls),2)
