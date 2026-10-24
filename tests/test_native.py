import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evolving_harness.native import dispatch, launch, native_tools
from evolving_harness.store import Store
from evolving_harness.evolution import ReplayProposer


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / 'state.db')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def call(self, operation, **values):
        return dispatch(self.store, 'alice', 'repo', {'operation': operation, **values})

    def test_memory_task_audit_and_scope(self):
        mid = self.call('memory-add', content='use unittest')['id']
        turn = self.call('begin', prompt='fix tests', session_id='pi-one')
        self.assertEqual(turn['memories'][0]['id'], mid)
        self.call('event', task_id=turn['task_id'], event={'toolName': 'bash', 'isError': False})
        self.call('finish', task_id=turn['task_id'], state='completed', text='done')
        self.assertEqual(self.store.get_task('alice', 'repo', turn['task_id'])['result'], 'done')
        with self.assertRaises(ValueError):
            dispatch(self.store, 'bob', 'repo', {'operation': 'event', 'task_id': turn['task_id'], 'event': {}})
        self.assertEqual(dispatch(self.store, 'bob', 'repo', {'operation': 'memory-list'})['memories'], [])
        with self.assertRaises(ValueError):
            self.call('finish', task_id=turn['task_id'], state='completed')

    def test_edit_forget_and_later_context(self):
        mid = self.call('memory-add', content='old')['id']
        self.call('memory-edit', id=mid, content='new')
        self.assertEqual(self.call('begin', prompt='work')['memories'][0]['content'], 'new')
        self.call('memory-forget', id=mid)
        self.assertEqual(self.call('begin', prompt='work')['memories'], [])

    def test_feedback_bound_to_task_and_evolution_requires_confirmation(self):
        task = self.call('begin', prompt='work')['task_id']
        self.call('feedback', defect='duplicate', note='same memory twice', task_id=task)
        self.assertEqual(len(self.call('evolution-preview')['evidence']), 1)
        with self.assertRaises(ValueError):
            self.call('evolve', pi_root='unused')

    def test_launcher_uses_native_ui_explicit_extension_and_absolute_database(self):
        entry = self.path / 'pi/packages/coding-agent/dist/bundle/cli.js'
        entry.parent.mkdir(parents=True)
        entry.touch()
        args = SimpleNamespace(workspace=str(self.path), pi_root=str(self.path / 'pi'),
            db=str(self.path / 'native.db'), user='alice', project='repo', allow_write=False,
            resume=True, provider=None, model=None)
        with patch('evolving_harness.native.resolve_node', return_value='node'), \
             patch('evolving_harness.native.subprocess.call', return_value=0) as run:
            self.assertEqual(launch(args), 0)
        command = run.call_args.args[0]
        self.assertNotIn('--mode', command)
        self.assertIn('--extension', command)
        self.assertIn('--continue', command)
        self.assertIn('--no-extensions', command)
        self.assertEqual(run.call_args.kwargs['env']['EH_PROJECT'], 'repo')

    def test_other_extensions_can_be_explicitly_enabled(self):
        entry = self.path / 'pi/packages/coding-agent/dist/bundle/cli.js'
        entry.parent.mkdir(parents=True)
        entry.touch()
        args = SimpleNamespace(workspace=str(self.path),pi_root=str(self.path/'pi'),db=str(self.path/'native.db'),
            user='alice',project='repo',allow_write=False,resume=False,provider=None,model=None,
            with_user_extensions=True,thinking='off')
        with patch('evolving_harness.native.resolve_node',return_value='node'), \
             patch('evolving_harness.native.subprocess.call',return_value=0) as run:
            launch(args)
        self.assertNotIn('--no-extensions',run.call_args.args[0])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--thinking')+1],'off')

    def test_native_evolution_gate_changes_future_retrieval_and_tracks_generator(self):
        self.call('memory-add', content='use unittest')
        self.call('memory-add', content='use unittest')
        before = self.call('begin', prompt='tests')
        self.assertEqual(len(before['memories']), 2)
        self.call('finish', task_id=before['task_id'], state='completed')
        self.call('feedback', defect='duplicate', note='duplicate context')
        # Scripted proposals exercise the real controller/gate; no model or performance claim.
        with patch('evolving_harness.native.PiProposer', return_value=ReplayProposer()):
            result = self.call('evolve', confirmed=True, apply=True, pi_root='unused')
        active = self.store.current('alice', 'repo', 'improve')
        self.assertGreater(active['version'], 1)
        self.assertTrue(any(c['generated_by'] == active['id'] for r in result['rounds'] for c in r['changes']))
        self.assertEqual(len(self.call('begin', prompt='tests')['memories']), 1)
        self.assertTrue(Path(result['report_path']).is_file())

    def test_windows_uses_powershell_without_missing_search_binaries(self):
        with patch('evolving_harness.native.shutil.which', return_value=None):
            self.assertEqual(native_tools(True, 'win32'), 'read,ls,powershell,edit,write')
            self.assertEqual(native_tools(False, 'win32'), 'read,ls')
            self.assertIn('bash', native_tools(True, 'linux'))

    def test_available_search_tools_are_preserved(self):
        with patch('evolving_harness.native.shutil.which', return_value='available'):
            self.assertTrue(native_tools(True, 'win32').endswith('grep,find'))
