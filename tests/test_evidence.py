import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from evolving_harness.native import dispatch
from evolving_harness.evidence import Evidence, snapshot, changes
from evolving_harness.store import Store


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.workspace = self.path / 'project'
        self.workspace.mkdir()
        self.store = Store(self.path / 'state.db')
        self.evidence = Evidence(self.store, 'alice', 'repo')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def call(self, operation, **values):
        return dispatch(self.store, 'alice', 'repo', {'operation':operation, **values})

    def begin(self):
        return self.call('begin', prompt='fix', workspace=str(self.workspace))['task_id']

    def test_trajectory_diff_redaction_claims_and_notes(self):
        code = self.workspace / 'app.py'
        code.write_text('value = 0\n', encoding='utf-8')
        task = self.begin()
        self.call('event', task_id=task, event={'phase':'start', 'toolCallId':'one',
                 'toolName':'edit', 'args': {'api_key':'dont-store'}})
        code.write_text('value = 1\n', encoding='utf-8')
        self.call('event', task_id=task, event={'phase':'end', 'toolCallId':'one', 'toolName':'edit',
                 'isError':False, 'output':'edited', 'thinking':'never retain'})
        self.call('finish', task_id=task, state='completed', text='All tests passed (model claim)')
        self.call('note', task_id=task, note='please check edge cases')
        bundle = self.call('task', task_id=task)
        self.assertEqual(bundle['summary']['latest_verification'], 'unverified')
        self.assertEqual(bundle['summary']['tool_records'], 1)
        self.assertEqual(bundle['summary']['changed_files'], ['app.py'])
        self.assertIn('-value = 0', bundle['changes'][0]['diff'])
        self.assertNotIn('dont-store', str(bundle))
        self.assertNotIn('never retain', str(bundle))
        self.assertTrue(any(e['kind']=='user_feedback' for e in bundle['events']))
        with self.assertRaises(ValueError):
            Evidence(self.store, 'bob', 'repo').bundle(task)

    def test_real_unittest_failure_then_pass_and_stale_fingerprint(self):
        test = self.workspace / 'test_sample.py'
        test.write_text('import unittest\nclass T(unittest.TestCase):\n def test_one(self): self.fail("baseline")\n', encoding='utf-8')
        task = self.begin()
        with patch.dict(os.environ, {'EH_ALLOW_WRITE':'1'}):
            failed = self.evidence.verify(task)
            test.write_text('import unittest\nclass T(unittest.TestCase):\n def test_one(self): self.assertEqual(1,1)\n', encoding='utf-8')
            passed = self.evidence.verify(task)
        self.assertEqual(failed['status'], 'failed')
        self.assertNotEqual(failed['exit_code'], 0)
        self.assertEqual(passed['status'], 'passed', passed['output'])
        self.assertEqual(passed['test_count'], 1)
        self.assertNotEqual(failed['before_snapshot']['manifest_hash'], passed['before_snapshot']['manifest_hash'])
        self.assertEqual(self.evidence.bundle(task)['summary']['verification_freshness'], 'matches_recorded_text_scope')
        test.write_text('# modified after testing\n', encoding='utf-8')
        self.assertEqual(self.evidence.bundle(task)['summary']['verification_freshness'], 'stale')

    def test_zero_tests_is_not_a_pass_and_permission_is_required(self):
        task = self.begin()
        with patch.dict(os.environ, {'EH_ALLOW_WRITE':'0'}):
            with self.assertRaises(ValueError):
                self.evidence.verify(task)
        with patch.dict(os.environ, {'EH_ALLOW_WRITE':'1'}):
            result = self.evidence.verify(task)
            self.assertEqual(result['status'], 'no_tests_or_unrecognized_output', result['output'])

    def test_sensitive_paths_and_large_files_are_excluded(self):
        (self.workspace / '.env').write_text('SECRET=do-not-read')
        (self.workspace / 'credentials.json').write_text('do-not-read')
        (self.workspace / 'huge.py').write_text('x' * 70000)
        (self.workspace / 'ok.py').write_text('answer=42')
        snap = snapshot(str(self.workspace))
        self.assertEqual(list(snap['files']), ['ok.py'])
        self.assertEqual(snap['coverage'], 'partial')
        self.assertNotIn('do-not-read', str(snap))

    def test_report_exports_do_not_change_code_fingerprint(self):
        before = snapshot(str(self.workspace))
        reports = self.workspace / 'native-reports'
        reports.mkdir()
        (reports / 'task.json').write_text('{"evidence":"exported"}')
        self.assertEqual(before['manifest_hash'], snapshot(str(self.workspace))['manifest_hash'])

    def test_standalone_verification_does_not_rewrite_legacy_task_evidence(self):
        legacy = self.call('begin', prompt='legacy')['task_id']
        self.call('finish', task_id=legacy, state='completed')
        with patch.dict(os.environ, {'EH_ALLOW_WRITE':'1'}):
            result = self.call('verify', workspace=str(self.workspace))
        self.assertNotEqual(result['task']['id'], legacy)
        self.assertEqual(result['summary']['verification_count'], 1)
        self.assertEqual(self.evidence.bundle(legacy)['summary']['verification_count'], 0)

    def test_legacy_task_remains_unverified(self):
        task = self.call('begin', prompt='legacy')['task_id']
        self.call('finish', task_id=task, state='completed', text='done')
        self.assertEqual(self.evidence.bundle(task)['summary']['latest_verification'], 'unverified')
        with patch.dict(os.environ, {'EH_ALLOW_WRITE':'1'}):
            with self.assertRaises(ValueError):
                self.evidence.verify(task)

    def test_timeout_and_code_change_are_not_passes(self):
        test = self.workspace / 'test_slow.py'
        test.write_text('import unittest,time\nclass T(unittest.TestCase):\n def test_slow(self): time.sleep(10)\n')
        task = self.begin()
        with patch.dict(os.environ, {'EH_ALLOW_WRITE':'1'}):
            self.assertEqual(self.evidence.verify(task, timeout=0.1)['status'], 'timeout')
        before = {'available':True, 'files':{}, 'manifest_hash':'one'}
        after = {'available':True, 'files':{}, 'manifest_hash':'two'}
        class FakeProcess:
            returncode = 0
            def wait(self, **kwargs): pass
        def popen(*args, **kwargs):
            kwargs['stdout'].write(b'Ran 1 test\nOK\n')
            return FakeProcess()
        with patch.dict(os.environ, {'EH_ALLOW_WRITE':'1'}), \
             patch('evolving_harness.evidence.snapshot', side_effect=[before,after]), \
             patch('evolving_harness.evidence.subprocess.Popen', side_effect=popen):
            self.assertEqual(self.evidence.verify(task)['status'], 'code_changed_during_test')
