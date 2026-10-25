"""Scripted actors exercise real checks; no external model evidence."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evolving_harness.components import Components
from evolving_harness.native import dispatch
from evolving_harness.store import Store
from evolving_harness.task_eval import TaskEvaluation, TaskPair, reference, validate, integrity
from evolving_harness.skill_compare import compare


class Actor:
    prompts = []
    fail_candidate = False

    def __init__(self, command, cwd, timeout, observe):
        self.root = Path(cwd); self.observe = observe

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def request(self, kind, **kwargs): return {'model': {'provider': 'fixture', 'id': 'fixture'}}

    async def prompt(self, prompt):
        self.prompts.append(prompt)
        candidate = 'CANDIDATE_GUIDANCE' in prompt
        content = 'def answer(): return ' + ('0' if candidate and self.fail_candidate else '42') + '\n'
        (self.root / 'app.py').write_text(content, encoding='utf-8')
        return {'text': '<script>not executed</script> Scripted actor; not a model.', 'message': {}}


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        (self.source / 'app.py').write_text('def answer(): return 1\n', encoding='utf-8')
        (self.source / 'AGENTS.md').write_text('Use unittest.\n', encoding='utf-8')
        (self.source / 'tests').mkdir()
        (self.source / 'tests/test_app.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_baseline(self):self.assertTrue(True)\n', encoding='utf-8')
        self.s = Store(self.root / 'state.db'); dispatch(self.s, 'alice', 'p', {'operation': 'status'})
        registry = Components(self.s, 'alice', 'p')
        self.candidate = registry.propose('skill:verify', {'kind': 'skill', 'description': 'Candidate',
            'triggers': ['fix'], 'steps': ['CANDIDATE_GUIDANCE']}, [], reason='Offline fixture')
        self.baseline = registry.propose('skill:old', {'kind': 'skill', 'description': 'Baseline',
            'triggers': ['fix'], 'steps': ['BASELINE_GUIDANCE']}, [], reason='Offline fixture')
        self.config = {'version': 1, 'name': 'Two tasks', 'baseline': self.baseline['id'], 'candidate': self.candidate['id'],
                       'cases': [self.case('first'), self.case('second')]}
        self.manifest = self.root / 'suite.json'; self.write_config()
        self.eval = TaskEvaluation(self.s, 'alice', 'p'); Actor.prompts = []; Actor.fail_candidate = False

    def case(self, name):
        return {'id': name, 'source': 'source', 'prompt': 'Implement answer returning 42. Run tests.', 'protected': [],
                'checks': [{'name': 'regression', 'args': ['-m', 'unittest', 'discover', '-s', 'tests', '-v'], 'require_tests': True},
                           {'name': 'independent-answer', 'args': ['-c', 'from app import answer; assert answer()==42'], 'require_tests': False}]}

    def write_config(self): self.manifest.write_text(json.dumps(self.config), encoding='utf-8')
    def tearDown(self): self.s.close(); self.temp.cleanup()

    def run_all(self, preview):
        with patch('evolving_harness.skill_compare.PiRpc', Actor), patch('evolving_harness.skill_compare.pi_command', return_value=['fixture']):
            for pair in preview['evaluation_preview']['pairs']:
                state = pair['evaluation_pair_preview']
                self.eval_run = asyncio.run(self.eval.run_case(state['suite_id'], state['id'], pair['packet_hash'], 'unused', 'fixture', 'fixture'))
        return self.eval_run

    def test_two_tasks_pin_both_versions_and_execute_real_checks(self):
        preview = self.eval.prepare(self.root, 'suite.json')
        result = self.run_all(preview); report = result['evaluation_report']
        self.assertTrue(report['complete']); self.assertEqual(report['counts'], {'tie_on_this_task': 2})
        self.assertEqual(report['recommendation'], 'keep_current'); self.assertFalse(report['automatic_activation'])
        self.assertEqual(len(Actor.prompts), 4)
        self.assertEqual(sum('BASELINE_GUIDANCE' in p for p in Actor.prompts), 2)
        self.assertEqual(sum('CANDIDATE_GUIDANCE' in p for p in Actor.prompts), 2)
        self.assertTrue(all('independent-answer' not in p for p in Actor.prompts))
        self.assertEqual((self.source / 'app.py').read_text(), 'def answer(): return 1\n')
        document = Path(result['report_html']).read_text(encoding='utf-8')
        self.assertNotIn('<script>', document); self.assertIn('&lt;script&gt;', document)
        self.assertEqual(json.loads(Path(result['report_json']).read_text(encoding='utf-8'))['id'], report['id'])

    def test_hyphenated_case_uses_discoverable_hint_and_rejects_skipped_new_test_file(self):
        self.config['cases'] = [self.case('case-with-hyphens')]; self.write_config()
        pair = self.eval.prepare(self.root, 'suite.json')['evaluation_preview']['pairs'][0]['evaluation_pair_preview']
        self.assertIn('tests/test_task_case_with_hyphens.py', pair['task'])
        self.assertIn('has no Git metadata', pair['task'])
        self.assertIn('Available tool names: ' + pair['available_tools'], pair['task'])
        root = Path(pair['arms']['with_skill']['workspace'])
        invalid = root / 'tests/test_task_case-with-hyphens.py'
        invalid.write_text('import unittest\nclass Added(unittest.TestCase):\n def test_new(self): self.assertTrue(True)\n', encoding='utf-8')
        check = reference(root, pair)
        self.assertEqual(check['status'], 'workspace_invalid')
        self.assertTrue(check['protected_intact'])
        self.assertEqual(check['violations'][0]['reason'], 'undiscoverable_test_filename')
        valid = invalid.with_name('test_task_case_with_hyphens.py'); invalid.rename(valid)
        (root / 'app.py').write_text('def answer(): return 42\n', encoding='utf-8')
        check = reference(root, pair)
        self.assertEqual(check['status'], 'passed'); self.assertEqual(check['test_count'], 2)
        # Historical frozen states retain their original grading policy.
        valid.rename(invalid); legacy = {k:v for k,v in pair.items() if k != 'test_discovery_policy'}
        self.assertEqual(reference(root, legacy)['status'], 'passed')

    def test_actual_regression_blocks_recommendation_and_attempt_cannot_replay(self):
        preview = self.eval.prepare(self.root, 'suite.json'); Actor.fail_candidate = True
        report = self.run_all(preview)['evaluation_report']
        self.assertEqual(report['counts'], {'worse_on_this_task': 2})
        self.assertEqual(report['recommendation'], 'do_not_promote')
        p = preview['evaluation_preview']['pairs'][0]; s = p['evaluation_pair_preview']
        with self.assertRaises(ValueError):
            asyncio.run(self.eval.run_case(s['suite_id'], s['id'], p['packet_hash'], '', 'fixture', 'fixture'))

    def test_protected_change_zero_tests_and_freeze(self):
        preview = self.eval.prepare(self.root, 'suite.json')
        s = preview['evaluation_preview']['pairs'][0]['evaluation_pair_preview']; root = Path(s['arms']['with_skill']['workspace'])
        (root / 'tests/test_app.py').write_text('')
        self.assertEqual(reference(root, s)['status'], 'protected_changed')
        s['protected'] = []; s['checks'] = [{'name': 'zero', 'args': ['-c', 'print("Ran 0 tests\\nOK")'], 'require_tests': True}]
        self.assertEqual(reference(root, s)['status'], 'failed')
        # Editing the original manifest cannot replace frozen checks.
        self.config['cases'][0]['checks'][0]['args'] = ['-c', 'print("bypass")']; self.write_config()
        self.assertNotIn('bypass', str(TaskPair(self.s, 'alice', 'p', s['id']).current()['checks']))

    def test_stale_copy_requires_new_preview(self):
        p = self.eval.prepare(self.root, 'suite.json')['evaluation_preview']['pairs'][0]; s = p['evaluation_pair_preview']
        Path(s['arms']['with_skill']['workspace'], 'app.py').write_text('changed')
        with self.assertRaises(ValueError):
            asyncio.run(self.eval.run_case(s['suite_id'], s['id'], p['packet_hash'], '', 'fixture', 'fixture'))

    def test_scope_confirmation_and_write_gates(self):
        preview = self.eval.prepare(self.root, 'suite.json')
        with self.assertRaises(ValueError): TaskEvaluation(self.s, 'bob', 'p').current()
        p = preview['evaluation_preview']['pairs'][0]; s = p['evaluation_pair_preview']
        request = {'operation': 'product-eval-run-case', 'suite_id': s['suite_id'], 'pair_id': s['id'], 'packet_hash': p['packet_hash'], 'pi_root': '', 'provider': 'fixture', 'model': 'fixture'}
        with self.assertRaises(ValueError): dispatch(self.s, 'alice', 'p', request)
        with patch.dict(os.environ, {'EH_ALLOW_WRITE': '0'}):
            with self.assertRaises(ValueError): dispatch(self.s, 'alice', 'p', request | {'confirmed': True})
        with self.assertRaises(ValueError):
            asyncio.run(self.eval.run_case(s['suite_id'], 'outside', p['packet_hash'], '', 'fixture', 'fixture'))

    def test_invalid_manifest_paths_and_identical_components(self):
        for value in ('../outside', '/absolute', 'C:/outside', '..\\outside'):
            with self.subTest(value=value):
                self.config['cases'][0]['source'] = value; self.write_config()
                with self.assertRaises(ValueError): self.eval.prepare(self.root, 'suite.json')
        self.config['cases'][0]['source'] = 'source'; self.config['baseline'] = self.candidate['id']; self.write_config()
        with self.assertRaises(ValueError): self.eval.prepare(self.root, 'suite.json')
        self.config['baseline'] = None; self.config['cases'][1]['id'] = 'first'
        with self.assertRaises(ValueError): validate(self.config)

    def test_partial_results_and_failed_runtime_never_become_improvement(self):
        preview = self.eval.prepare(self.root, 'suite.json')
        report = self.eval.report()['evaluation_report']
        self.assertFalse(report['complete']); self.assertEqual(report['recommendation'], 'pending')
        p = preview['evaluation_preview']['pairs'][0]; s = p['evaluation_pair_preview']
        with patch('evolving_harness.skill_compare.pi_command', return_value=['fixture']), patch.object(Actor, 'prompt', side_effect=RuntimeError('fixture timeout')), patch('evolving_harness.skill_compare.PiRpc', Actor):
            report = asyncio.run(self.eval.run_case(s['suite_id'], s['id'], p['packet_hash'], '', 'fixture', 'fixture'))['evaluation_report']
        self.assertEqual(report['cases'][0]['outcome'], 'inconclusive')
        self.assertEqual(report['recommendation'], 'pending')

    def test_enrolled_evaluation_blocks_activation_until_complete_and_rejects_regression(self):
        preview = self.eval.prepare(self.root, 'suite.json')
        registry = Components(self.s, 'alice', 'p'); gate = registry.evaluate(self.candidate['id'])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            registry.promote(self.candidate['id'], gate['evaluation_id'])
        Actor.fail_candidate = True; self.run_all(preview)
        with self.assertRaisesRegex(ValueError, 'regressed'):
            registry.promote(self.candidate['id'], gate['evaluation_id'])
        # A fresh passing suite for the same immutable candidate cannot erase a failed attempt.
        Actor.fail_candidate = False; self.run_all(self.eval.prepare(self.root, 'suite.json'))
        with self.assertRaisesRegex(ValueError, 'regressed'):
            registry.promote(self.candidate['id'], gate['evaluation_id'])

    def test_tied_completed_evaluation_still_needs_contract_gate_and_explicit_promotion(self):
        self.run_all(self.eval.prepare(self.root, 'suite.json'))
        registry = Components(self.s, 'alice', 'p')
        self.assertIsNone(registry.active('skill:verify'))
        gate = registry.evaluate(self.candidate['id'])
        promoted = registry.promote(self.candidate['id'], gate['evaluation_id'])
        self.assertEqual(promoted['id'], self.candidate['id'])

    def test_manifest_inside_source_is_not_copied_as_scoring_hint(self):
        self.config['cases'] = [self.case('root-case')]
        self.config['cases'][0]['source'] = '.'; self.write_config()
        state = self.eval.prepare(self.root, 'suite.json')['evaluation_preview']['pairs'][0]['evaluation_pair_preview']
        self.assertNotIn('suite.json', state['files'])
        self.assertFalse(Path(state['arms']['with_skill']['workspace'], 'suite.json').exists())
        self.assertEqual(TaskPair(self.s, 'alice', 'p', state['id']).collect(state['source']), state['files'])

    def test_new_test_file_is_allowed_but_append_to_existing_test_is_explained(self):
        state = self.eval.prepare(self.root, 'suite.json')['evaluation_preview']['pairs'][0]['evaluation_pair_preview']
        self.assertIn('Add regression tests only in NEW files', state['task'])
        self.assertIn('append tests to', state['task'])
        root = Path(state['arms']['with_skill']['workspace'])
        (root / 'tests/test_new.py').write_text('import unittest\nclass New(unittest.TestCase):\n def test_new(self):self.assertTrue(True)\n')
        self.assertTrue(integrity(root, state)['integrity_ok'])
        target = root / 'tests/test_app.py'
        target.write_text(target.read_text() + '\n# additional assertion would still violate the file protection\n')
        with patch('evolving_harness.task_eval.subprocess.Popen') as child:
            result = reference(root, state)
        child.assert_not_called()
        self.assertEqual(result['status'], 'protected_changed')
        issue = next(v for v in result['violations'] if v['path'] == 'tests/test_app.py')
        self.assertEqual(issue['reason'], 'changed')
        self.assertNotEqual(issue['actual_hash'], issue['expected_hash'])
        self.assertNotIn('test_baseline', json.dumps(issue))

    def test_missing_protected_file_and_workspace_limit_have_distinct_diagnostics(self):
        state = self.eval.prepare(self.root, 'suite.json')['evaluation_preview']['pairs'][0]['evaluation_pair_preview']
        root = Path(state['arms']['with_skill']['workspace']); protected = root / 'AGENTS.md'
        original = protected.read_text(); protected.unlink()
        result = reference(root, state)
        self.assertIn({'path': 'AGENTS.md', 'reason': 'missing', 'protected': True}, result['violations'])
        protected.write_text(original)
        (root / 'oversized.txt').write_text('x' * 128001)
        result = reference(root, state)
        self.assertEqual(result['status'], 'workspace_invalid')
        self.assertTrue(result['protected_intact']); self.assertFalse(result['integrity_ok'])
        arm = {'status': 'completed', 'reference': result, 'metrics': {'isolated_check_observed': False}}
        self.assertEqual(compare({'without_skill': arm, 'with_skill': arm})['outcome'], 'inconclusive')

    def test_checker_side_effect_on_protected_file_is_reported_after_execution(self):
        state = self.eval.prepare(self.root, 'suite.json')['evaluation_preview']['pairs'][0]['evaluation_pair_preview']
        root = Path(state['arms']['with_skill']['workspace'])
        state['checks'] = [{'name': 'invalid-checker-fixture', 'args': ['-c',
            "from pathlib import Path; Path('AGENTS.md').write_text('changed'); print('Ran 1 test\\nOK')"], 'require_tests': True}]
        result = reference(root, state)
        self.assertEqual(result['status'], 'protected_changed')
        self.assertFalse(result['snapshot_stable'])
        self.assertEqual(result['violations'][0]['path'], 'AGENTS.md')


if __name__ == '__main__': unittest.main()
