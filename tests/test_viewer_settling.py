"""Bounded read-only settling with synthetic Drive responses; no live uploads."""
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch

from uqmc_recovery import destination_publication as p
from test_destination_publication import FakeDrive, DestinationPublicationTests


class ViewerSettlingTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(p, 'sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def versions(self, drive, versions):
        drive.metadata = Mock(side_effect=[{**copy.deepcopy(drive.meta), 'version': str(v)} for v in versions])

    def publication(self):
        f = DestinationPublicationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f, f.freeze()

    def test_stable_reads_do_not_wait(self):
        drive = FakeDrive(b'<html>current</html>')
        self.versions(drive, [1, 1])
        self.assertEqual(p._read_viewer(drive, 'viewer')[1], drive.content)
        self.sleep.assert_not_called()

    def test_version_settles_after_two_delayed_reads(self):
        drive = FakeDrive(b'<html>current</html>')
        self.versions(drive, [1, 2, 2, 3, 3, 3])
        self.assertEqual(p._read_viewer(drive, 'viewer')[1], drive.content)
        self.assertEqual(self.sleep.call_args_list, [call(3), call(3)])
        self.assertEqual(drive.metadata.call_count, 6)
        self.assertEqual(drive.writes, [])

    def test_continuing_version_change_has_a_fixed_limit(self):
        drive = FakeDrive(b'<html>current</html>')
        self.versions(drive, range(6))
        with self.assertRaisesRegex(ValueError, 'VIEWER_VERSION_CHANGED_DURING_READ'):
            p._read_viewer(drive, 'viewer')
        self.assertEqual(self.sleep.call_args_list, [call(3), call(3)])
        self.assertEqual(drive.metadata.call_count, 6)

    def test_identity_change_between_attempts_is_not_adopted(self):
        for change in ({'name': 'Edited.html'}, {'parents': ['different-parent']}):
            with self.subTest(change=change):
                drive = FakeDrive(b'<html>current</html>')
                drive.metadata = Mock(side_effect=[copy.deepcopy(drive.meta),
                    {**drive.meta, 'version': '2'}, {**drive.meta, 'version': '2', **change}])
                with self.assertRaisesRegex(ValueError, 'VIEWER_CHANGED_DURING_READ'):
                    p._read_viewer(drive, 'viewer')
                self.assertEqual(drive.metadata.call_count, 3)
                self.assertEqual(drive.writes, [])

    def test_invalid_content_and_identity_are_not_retried(self):
        drive = FakeDrive('not bytes')
        with self.assertRaisesRegex(ValueError, 'VIEWER_DOWNLOAD_NOT_BYTES'):
            p._read_viewer(drive, 'viewer')
        drive = FakeDrive(b'<html>current</html>')
        drive.meta['trashed'] = True
        with self.assertRaisesRegex(ValueError, 'VIEWER_IDENTITY_OR_TRASH_CONFLICT'):
            p._read_viewer(drive, 'viewer')
        self.sleep.assert_not_called()

    def test_upload_wait_precedes_first_readback(self):
        f, path = self.publication()
        metadata = f.drive.metadata
        def checked(fid):
            if f.drive.writes:
                f.sleep.assert_called_once_with(3)
            return metadata(fid)
        f.drive.metadata = checked
        result = f.run_publish(path)
        self.assertEqual(result['result'], 'PUBLICATION_VERIFIED')
        self.assertEqual(len(f.drive.writes), 1)

    def test_response_loss_and_one_version_bump_do_not_reupload(self):
        f, path = self.publication()
        f.drive.fail = 'timeout'
        metadata = f.drive.metadata
        reads = 0
        def settling(fid):
            nonlocal reads
            if f.drive.writes:
                reads += 1
                if reads == 2:
                    f.drive.meta['version'] = '3'
            return metadata(fid)
        f.drive.metadata = settling
        result = f.run_publish(path)
        self.assertEqual(result['result'], 'PUBLICATION_VERIFIED')
        self.assertEqual(result['html']['write_error_class'], 'TimeoutError')
        self.assertEqual(f.sleep.call_args_list, [call(3), call(3)])
        self.assertEqual(len(f.drive.writes), 1)

    def test_unsettled_upload_stays_uncertain_and_resume_only_reads(self):
        f, path = self.publication()
        metadata = f.drive.metadata
        def unsettled(fid):
            if f.drive.writes:
                f.drive.meta['version'] = str(int(f.drive.meta['version']) + 1)
            return metadata(fid)
        f.drive.metadata = unsettled
        first = f.run_publish(path)
        self.assertEqual(first['html']['state'], 'UNCERTAIN')
        self.assertEqual(first['html']['reason'], 'VIEWER_VERSION_CHANGED_DURING_READ')
        self.assertEqual(f.sleep.call_args_list, [call(3), call(3), call(3)])
        f.drive.metadata = metadata
        self.assertEqual(f.run_publish(path)['result'], 'PUBLICATION_VERIFIED')
        self.assertEqual(len(f.drive.writes), 1)
        self.assertEqual(len(f.sheet.updates), 1)

    def test_other_content_after_settling_remains_conflict(self):
        f, path = self.publication()
        f.drive.content = b'<html>Human edit</html>'
        self.versions(f.drive, [1, 2, 2, 2])
        result = f.run_publish(path)
        self.assertEqual(result['html']['state'], 'CONFLICT')
        self.assertEqual(result['html']['reason'], 'VIEWER_CONTENT_CHANGED')
        self.assertEqual(f.drive.writes, [])

    def test_nonmatching_upload_readback_cannot_verify_or_reupload(self):
        f, path = self.publication()
        replace = f.drive.replace_content
        def concurrent_edit(fid, content):
            replace(fid, content)
            f.drive.content = b'<html>Concurrent human edit</html>'
        f.drive.replace_content = concurrent_edit
        first = f.run_publish(path)
        self.assertEqual(first['html']['state'], 'UNCERTAIN')
        self.assertEqual(first['html']['reason'], 'UPLOAD_NOT_VERIFIED')
        self.assertEqual(f.run_publish(path)['html']['state'], 'CONFLICT')
        self.assertEqual(len(f.drive.writes), 1)


if __name__ == '__main__':
    unittest.main()
