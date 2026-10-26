import io
from datetime import datetime, timedelta, timezone
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from uqmc_recovery.destination_console import ConsoleReporter
from uqmc_recovery.destination import main
from uqmc_recovery.model import SafetyError


class DestinationConsoleTests(unittest.TestCase):
    def reporter(self, **kwargs):
        self.out = io.StringIO()
        self.r = ConsoleReporter(stream=self.out, clock=lambda: 30.0, **kwargs)
        self.addCleanup(self.r.close)
        return self.r

    def test_human_lines_include_local_wall_clock_timezone_and_elapsed(self):
        wall=datetime(2026,10,26,18,17,38,tzinfo=timezone(timedelta(hours=10),'AEST'))
        r=self.reporter(wall_clock=lambda:wall)
        r.progress({'stage':'folder_verified','path':'Club/Skills'})
        self.assertEqual(self.out.getvalue(),'[00:00] [18:17:38 AEST +1000] Folder verified: Club/Skills\n')

    def test_wall_clock_refreshes_each_line_without_changing_monotonic_elapsed(self):
        ticks=iter([100.0,125.0,133.0])
        wall=iter([datetime(2026,4,5,2,59,59,tzinfo=timezone(timedelta(hours=11),'AEDT')),
                   datetime(2026,4,5,2,0,1,tzinfo=timezone(timedelta(hours=10),'AEST'))])
        out=io.StringIO()
        r=ConsoleReporter(stream=out,clock=lambda:next(ticks),wall_clock=lambda:next(wall))
        r.line('before');r.line('after')
        self.assertEqual(out.getvalue().splitlines(),[
            '[00:25] [02:59:59 AEDT +1100] before',
            '[00:33] [02:00:01 AEST +1000] after'])

    def test_json_output_never_adds_human_wall_clock_prefix(self):
        def unused():raise AssertionError('JSON output sampled local wall clock')
        r=self.reporter(json_output=True,wall_clock=unused)
        values=[{'stage':'folder_verified','path':'Club/Skills'}, {'error':'READ_TIMEOUT'}, {'counts':{'APPLIED':1}}]
        r.progress(values[0]);r.error(values[1]);r.finish(values[2])
        self.assertEqual([json.loads(line) for line in self.out.getvalue().splitlines()],values)

    def test_private_transcript_retains_utc_and_existing_field_names(self):
        wall=datetime(2026,10,26,18,17,38,tzinfo=timezone(timedelta(hours=10),'AEST'))
        r=self.reporter(wall_clock=lambda:wall)
        with tempfile.TemporaryDirectory() as directory:
            r.open_log(directory);r.progress({'stage':'folder_verified','path':'Club/Skills'})
            row=json.loads(r.log_path.read_text())
            self.assertEqual(set(row),{'type','at_utc','elapsed_seconds','data'})
            self.assertTrue(row['at_utc'].endswith('Z'))
            self.assertEqual(row['elapsed_seconds'],0.0)
            self.assertEqual(row['data'],{'stage':'folder_verified','path':'Club/Skills'})

    def test_readable_completion_names_counts_elapsed_and_publication(self):
        r = self.reporter()
        r.progress({'event':'item_started', 'mode':'APPLY', 'total_items':2})
        r.progress({'event':'item_finished','completed_items':1,'total_items':2,
                    'state':'APPLIED','observed':{'name':'Certificate.pdf'},
                    'raw_response':'private detail'})
        r.finish({'counts':{'APPLIED':2}, 'journal_sequence':42,
                  'publication':{'state':'PENDING','error':'READ_TIMEOUT'}})
        text = self.out.getvalue()
        self.assertIn('[00:00]', text)
        self.assertIn('1/2 applied: Certificate.pdf', text)
        self.assertIn('2 applied', text)
        self.assertIn('Publication: PENDING; READ_TIMEOUT', text)
        self.assertNotIn('private detail', text)
        self.assertNotIn('"counts"', text)

    def test_runner_conflict_supplies_name_without_claiming_observation(self):
        import copy
        import threading
        from uqmc_recovery.desired import DesiredRunner
        r = self.reporter()
        runner = object.__new__(DesiredRunner)
        runner.by_id = {'fixture-shortcut': {'target': {'name': "Club Photos"}}}
        runner._progress_gate = threading.RLock()
        runner.progress = r.progress
        runner.progress_output_failed = False
        result = {'source_id': 'fixture-shortcut', 'state': 'CONFLICT',
                  'reason': 'PROTECTED_METADATA_DRIFT'}
        before = copy.deepcopy(result)
        runner.report_progress('item_finished', completed_items=1, total_items=1, **result)
        self.assertIn('1/1 conflict: Club Photos; PROTECTED_METADATA_DRIFT (ID: fixture-shortcut)',
                      self.out.getvalue())
        self.assertEqual(result, before)
        self.assertNotIn('observed', result)

    def test_conflict_observed_name_wins_over_planned_display_name(self):
        r = self.reporter()
        r.progress({'event': 'item_finished', 'state': 'CONFLICT',
                    'completed_items': 1, 'total_items': 1, 'source_id': 'fixture',
                    'observed': {'name': 'Actual current name'}, 'display_name': 'Planned name',
                    'reason': 'CONCURRENT_CHANGE'})
        self.assertIn('conflict: Actual current name; CONCURRENT_CHANGE (ID: fixture)', self.out.getvalue())

    def test_actual_nested_publication_result_shows_both_verified_stages(self):
        r=self.reporter()
        r.finish({'counts':{'APPLIED':320},'publication':{
            'result':'PUBLICATION_VERIFIED','snapshot_id':'fixture-snapshot',
            'sheet':{'state':'VERIFIED','result':{'result':'PUBLICATION_VERIFIED'}},
            'html':{'state':'VERIFIED','sha256':'fixture'}}})
        text=self.out.getvalue()
        self.assertIn('Publication: PUBLICATION_VERIFIED.',text)
        self.assertIn('Spreadsheet: VERIFIED.',text)
        self.assertIn('Drive HTML: VERIFIED.',text)
        self.assertNotIn('UNKNOWN',text)

    def test_actual_publish_only_partial_result_shows_stage_reason(self):
        r=self.reporter()
        r.finish({'result':'PUBLICATION_INCOMPLETE','journal_sequence':42,
                  'sheet':{'state':'VERIFIED'},
                  'html':{'state':'CONFLICT','reason':'VIEWER_CONTENT_CHANGED'}})
        text=self.out.getvalue()
        self.assertIn('Publication: PUBLICATION_INCOMPLETE.',text)
        self.assertIn('Spreadsheet: VERIFIED.',text)
        self.assertIn('Drive HTML: CONFLICT; VIEWER_CONTENT_CHANGED.',text)

    def test_nested_sheet_failure_reason_without_raw_error(self):
        r=self.reporter()
        r.finish({'publication':{'result':'PUBLICATION_INCOMPLETE',
            'sheet':{'state':'CONFLICT','result':{'result':'PUBLICATION_CONFLICT','reason':'HUMAN_EDIT_CONFLICT','raw':'private'}},
            'html':{'state':'BLOCKED','error_class':'TransportError'}}})
        text=self.out.getvalue()
        self.assertIn('Spreadsheet: CONFLICT; HUMAN_EDIT_CONFLICT.',text)
        self.assertIn('Drive HTML: BLOCKED; TransportError.',text)
        self.assertNotIn('private',text)

    def test_publication_validation_reason_is_actionable_not_only_exception_class(self):
        from uqmc_recovery.destination_publication import _error
        detail = _error(ValueError('SECTION_IDENTITIES_NOT_PUBLIC_ALLOWLISTED'))
        r = self.reporter()
        r.finish({'counts': {'APPLIED': 3}, 'publication': {
            'state': 'PENDING', 'error': detail['reason'], 'error_class': 'ValueError'}})
        text = self.out.getvalue()
        self.assertIn('Publication: PENDING; SECTION_IDENTITIES_NOT_PUBLIC_ALLOWLISTED', text)
        self.assertIn('section membership conflicts with the public display allowlist', text)
        self.assertIn('Saved migration results are unchanged', text)
        self.assertNotIn('ValueError', text)

    def test_json_mode_keeps_original_values(self):
        r = self.reporter(json_output=True)
        progress = {'event':'item_finished','state':'APPLIED','source_id':'fixture'}
        result = {'counts':{'APPLIED':1},'publication':{'state':'VERIFIED'}}
        r.progress(progress);r.finish(result)
        self.assertEqual([json.loads(x) for x in self.out.getvalue().splitlines()], [progress,result])

    def test_full_private_transcript_even_when_human_line_suppressed(self):
        r = self.reporter()
        with tempfile.TemporaryDirectory() as directory:
            r.open_log(directory)
            value = {'stage':'source_metadata','checked':2,'total':20,'full_detail':['kept']}
            r.progress(value);r.finish({'counts':{'COMPLETE':1}})
            rows = [json.loads(x) for x in r.log_path.read_text().splitlines()]
            self.assertEqual(rows[0]['data'], value)
            self.assertEqual(rows[1]['type'], 'result')
            self.assertEqual(stat.S_IMODE(r.log_path.stat().st_mode), 0o600)
            self.assertNotIn('Checking source metadata: 2/', self.out.getvalue())

    def test_metadata_throttled_but_first_and_last_shown(self):
        r = self.reporter()
        for i in range(1,24):r.progress({'stage':'source_metadata','checked':i,'total':23})
        text = self.out.getvalue()
        self.assertEqual(text.count('Checking source metadata:'),4)
        self.assertIn('1/23', text);self.assertIn('23/23', text)

    def test_error_is_prompt_and_does_not_dump_structured_evidence(self):
        r = self.reporter()
        r.error({'error':'DRIVE_REFUSED', 'request_error':{'raw_evidence':'private'}})
        self.assertIn('Error: DRIVE_REFUSED',self.out.getvalue())
        self.assertNotIn('raw_evidence',self.out.getvalue())

    def test_newlines_control_sequences_and_long_name_do_not_break_lines(self):
        r = self.reporter()
        r.progress({'event':'item_finished','state':'APPLIED','completed_items':1,'total_items':1,
                    'observed':{'name':'long\nname\t\x1b'+'a'*200}})
        self.assertEqual(len(self.out.getvalue().splitlines()),1)
        self.assertIn('…',self.out.getvalue())
        self.assertNotIn('\x1b',self.out.getvalue())

    def test_log_write_error_warns_once_without_raising(self):
        r = self.reporter()
        with tempfile.TemporaryDirectory() as directory:
            r.open_log(directory)
            with patch('uqmc_recovery.destination_console.os.fsync',side_effect=OSError('disk')):
                r.progress({'stage':'account_verified','account':'fixture@example.test'})
                r.progress({'stage':'fresh_sheet','section':'Club','selected_identities':1})
            self.assertEqual(self.out.getvalue().count('transcript write failed'),1)
            self.assertIn('Fresh plan: Club',self.out.getvalue())

    def test_main_default_and_json_do_not_change_execution_arguments(self):
        for flag in ([],['--json']):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                cfg = Path(directory)/'config.json'; cfg.write_text(json.dumps({'run_directory':directory}))
                out=io.StringIO()
                def run(config, section, **kwargs):
                    self.assertEqual(section,'Club/Skills');self.assertTrue(kwargs['apply'])
                    kwargs['report']({'stage':'folder_verified','path':'Club/Skills','source_id':'fixture'})
                    return {'counts':{'APPLIED':1},'publication':{'state':'VERIFIED'}}
                with patch('uqmc_recovery.destination.run_destination',side_effect=run),redirect_stdout(out):
                    self.assertEqual(main(['Club/Skills','--config',str(cfg),'--apply',*flag]),0)
                logs=list((Path(directory)/'console-logs').glob('*.jsonl'))
                self.assertEqual(len(logs),1)
                rows=[json.loads(x) for x in logs[0].read_text().splitlines()]
                self.assertEqual([x['type'] for x in rows],['progress','result'])
                if flag:self.assertEqual(json.loads(out.getvalue().splitlines()[-1])['counts'],{'APPLIED':1})
                else:self.assertIn('Publication: VERIFIED',out.getvalue())

    def test_main_preserves_failure_exit_and_logs_safe_error(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=Path(directory)/'config.json';cfg.write_text(json.dumps({'run_directory':directory}))
            out=io.StringIO()
            with patch('uqmc_recovery.destination.run_destination',side_effect=SafetyError('BLOCKED')),redirect_stdout(out):
                self.assertEqual(main(['Club','--config',str(cfg)]),2)
            self.assertIn('Error: BLOCKED',out.getvalue())
            rows=[json.loads(x) for x in next((Path(directory)/'console-logs').glob('*.jsonl')).read_text().splitlines()]
            self.assertEqual(rows[-1]['data'],{'error':'BLOCKED'})


if __name__=='__main__':unittest.main()

class ResumeConsoleTests(unittest.TestCase):
    reporter = DestinationConsoleTests.reporter
    def test_resume_and_revision_error_are_concise_and_actionable(self):
        r=self.reporter()
        r.progress({'stage':'resume_selection','skipped_completed_items':12,'queued_items':3,
            'retained_post_completion_conflicts':[{'source_id':'fixture','display_name':'Register',
                'fields':['modifiedTime','size']}]})
        r.progress({'stage':'scheduling_stopped','reason':'WORKBOOK_REVISION_CHECK_UNAVAILABLE',
            'request_error':{'code':'AUTH_REFRESH_HTTP_ERROR','request':'oauth.refresh','status':503,'read_attempts':3}})
        text=self.out.getvalue()
        self.assertIn('skipping 12 previously verified items; 3 unfinished items queued',text)
        self.assertIn('Register; modifiedTime, size. No repeat mutation.',text)
        self.assertIn('AUTH_REFRESH_HTTP_ERROR; oauth.refresh; HTTP 503; attempts 3',text)
