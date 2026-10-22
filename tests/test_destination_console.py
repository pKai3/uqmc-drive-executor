import io
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
