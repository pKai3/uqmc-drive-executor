"""Old preview GETs lack marker evidence; confirmed absences remain conflicts."""
import unittest
from unittest.mock import patch

import test_destination_command as fixture
import test_destination_created_recovery as recovery_fixture
from uqmc_recovery.model import SafetyError


class RecoveryMarkerCacheTests(unittest.TestCase):
    def prepared(self):
        helper=recovery_fixture.CreatedDestinationRecoveryTests()
        self.addCleanup(helper.doCleanups)
        f=helper.fixture()
        return f,helper.interrupt_after_folders(f)

    def cache_response(self,f,child,mode):
        original=fixture.Client.get_file
        def response(client,sid):
            raw=original(client,sid)
            if sid==child:
                if mode=='wrong':raw['appProperties']={'uqmcFolderKey':'wrong-marker'}
                else:raw.pop('appProperties',None)
                if mode=='confirmed_missing':raw['_app_properties_requested']=True
            return raw
        with patch.object(fixture.Client,'get_file',response):
            with self.assertRaisesRegex(SafetyError,'RECOVERED_DESTINATION_METADATA_CONFLICT'):
                f.run_command()
        f.server.reads.clear()

    def test_old_unrequested_marker_refreshes_only_affected_metadata(self):
        f,child=self.prepared();self.cache_response(f,child,'not_requested')
        result=f.run_command()
        self.assertTrue(result['preview_only'])
        self.assertEqual([sid for method,sid in f.server.reads if method=='get'],[child])
        self.assertEqual(f.server.requests,[])

    def test_confirmed_absent_marker_stays_blocked_without_repeated_read(self):
        f,child=self.prepared();self.cache_response(f,child,'confirmed_missing')
        with self.assertRaisesRegex(SafetyError,'RECOVERED_DESTINATION_METADATA_CONFLICT'):
            f.run_command()
        self.assertNotIn(('get',child),f.server.reads)
        self.assertEqual(f.server.requests,[])

    def test_different_marker_stays_blocked_without_repeated_read(self):
        f,child=self.prepared();self.cache_response(f,child,'wrong')
        with self.assertRaisesRegex(SafetyError,'RECOVERED_DESTINATION_METADATA_CONFLICT'):
            f.run_command()
        self.assertNotIn(('get',child),f.server.reads)
        self.assertEqual(f.server.requests,[])


if __name__=='__main__':unittest.main()
