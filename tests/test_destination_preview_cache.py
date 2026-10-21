"""Read-only preview recovery fixtures; no Google or private project data."""
import copy
import unittest
from unittest.mock import patch

import test_destination_command as fixtures
from uqmc_recovery.destination import read_and_plan
from uqmc_recovery.direct_transport import TransportError
from uqmc_recovery.importer import HEADERS


class DestinationPreviewCacheTests(unittest.TestCase):
    setUp = fixtures.DestinationCommandTests.setUp
    publisher = fixtures.DestinationCommandTests.publisher
    run_command = fixtures.DestinationCommandTests.run_command

    def test_same_input_reuses_readonly_cache_only_within_five_minutes(self):
        with patch('uqmc_recovery.destination.time.time',return_value=1000):
            self.run_command()
        self.server.reads.clear()
        with patch('uqmc_recovery.destination.time.time',return_value=1299):
            result=self.run_command()
        self.assertGreater(result['preview_cached_reads_available'],0)
        self.assertNotIn(('get','leaf'),self.server.reads)
        self.assertNotIn(('get','source-root'),self.server.reads)
        self.server.reads.clear()
        with patch('uqmc_recovery.destination.time.time',return_value=1301):
            result=self.run_command()
        self.assertEqual(result['preview_cached_reads_available'],0)
        self.assertIn(('get','leaf'),self.server.reads)
        self.assertIn(('get','source-root'),self.server.reads)
        self.assertEqual(self.capture.calls,3)
        self.assertEqual(self.server.requests,[])

    def test_changed_fresh_proposal_fingerprint_invalidates_cached_reads(self):
        self.run_command();self.server.reads.clear()
        col=HEADERS['Proposed Renames'].index('Proposed name + type suffix')
        self.export['sheets']['Proposed Renames'][2][col]='Revised approved.pdf'
        result=self.run_command()
        self.assertEqual(result['preview_cached_reads_available'],0)
        self.assertIn(('get','leaf'),self.server.reads)
        self.assertIn(('get','source-root'),self.server.reads)
        self.assertEqual(self.capture.calls,2)
        self.assertEqual(self.server.requests,[])

    def test_apply_ignores_preview_cache_and_observes_name_and_capability_drift(self):
        self.run_command();self.server.reads.clear()
        self.server.drive.files['leaf']['name']='Concurrent edit.pdf'
        self.server.drive.files['leaf']['capabilities']['canRename']=False
        observed=[]
        def collect(*args,**kwargs):
            value=read_and_plan(*args,**kwargs)
            observed.append(copy.deepcopy(value['live']))
            return value
        with patch('uqmc_recovery.destination.read_and_plan',side_effect=collect):
            result=self.run_command(apply=True)
        self.assertEqual(result['preview_cached_reads_available'],0)
        self.assertIn(('get','leaf'),self.server.reads)
        self.assertEqual(observed[0]['raw']['leaf']['name'],'Concurrent edit.pdf')
        self.assertFalse(observed[0]['raw']['leaf']['capabilities']['canRename'])
        self.assertTrue(result['blockers'])
        self.assertEqual(self.server.requests,[])
        self.assertEqual(self.server.created,[])

    def test_transient_read_stops_at_two_and_permanent_403_is_not_retried(self):
        original=self.server.factory
        for code,status,expected in [('DRIVE_REQUEST_TIMEOUT',None,2),('DRIVE_HTTP_ERROR',403,1)]:
            with self.subTest(code=code,status=status):
                attempts=[];self.progress.clear()
                def factory(allowed,mutations,excluded,enabled,**kwargs):
                    client=original(allowed,mutations,excluded,enabled,**kwargs)
                    listing=client.list_children
                    def fail(sid):
                        if sid=='old-parent':
                            attempts.append(sid)
                            raise TransportError(code,status=status,request='files.list',item_id=sid)
                        return listing(sid)
                    client.list_children=fail
                    return client
                self.server.factory=factory
                with self.assertRaises(TransportError) as error:
                    self.run_command()
                self.assertEqual(len(attempts),expected)
                self.assertEqual(error.exception.request,'files.list')
                self.assertEqual(error.exception.item_id,'old-parent')
                retries=[x for x in self.progress if x.get('stage')=='read_retry']
                self.assertEqual(len(retries),expected-1)
                if retries:
                    self.assertEqual(retries[0],{'stage':'read_retry','method':'list_children',
                        'source_id':'old-parent','attempt':2,'reason':code})
        self.server.factory=original;self.server.reads.clear()
        result=self.run_command()
        self.assertGreater(result['preview_cached_reads_available'],0)
        self.assertNotIn(('get','leaf'),self.server.reads)
        self.assertNotIn(('get','source-root'),self.server.reads)
        self.assertEqual(self.server.reads.count(('list','old-parent')),1)
        self.assertEqual(self.server.requests,[])


if __name__=='__main__':
    unittest.main()
