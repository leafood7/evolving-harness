"""Real subprocess grading of authored fixtures; never a model comparison."""
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.log_inspector.calibrate import calibrate, fingerprint
from benchmarks.log_inspector.checks import PROBES, probe_source
from benchmarks.log_inspector.fixtures import apply
from benchmarks.log_inspector.prepare_pilot import prepare, task_prompt
from evolving_harness.domain import digest
from evolving_harness.store import Store
from evolving_harness.task_eval import collect, TaskEvaluation

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'benchmarks/log_inspector/source'


class LogBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calibration = calibrate(SOURCE)

    def test_all_ten_graders_accept_correct_and_reject_wrong_and_unimplemented(self):
        self.assertTrue(self.calibration['passed'], self.calibration)
        self.assertTrue(self.calibration['source_unchanged'])
        self.assertEqual(len(self.calibration['tasks']), 10)
        for row in self.calibration['tasks']:
            self.assertEqual(row['checks']['correct']['exit_code'], 0)
            self.assertNotEqual(row['checks']['wrong']['exit_code'], 0)
            self.assertNotEqual(row['checks']['unchanged']['exit_code'], 0)
            self.assertIn('Ran 31 tests', row['checks']['correct_regression']['output'])

    def test_calibration_cannot_patch_unmarked_source_and_probes_fit_frozen_schema(self):
        before = fingerprint(SOURCE)
        with self.assertRaises(ValueError): apply(SOURCE, 'dev-01')
        self.assertEqual(before, fingerprint(SOURCE))
        for key in PROBES:
            self.assertLessEqual(len(probe_source(key)), 8000)

    def test_pilot_uses_fresh_scope_and_only_development_tasks_without_running(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); preparation = root / 'prep'; preparation.mkdir()
            shutil.copytree(SOURCE, preparation / 'frozen-source')
            payload = {'kind': 'skill', 'description': 'Frozen fixture', 'triggers': ['fix'], 'steps': ['Verify default tests.']}
            candidate = payload | {'steps': ['Verify default tests; isolated verification only for changed imports.']}
            files = collect(SOURCE)
            docs = {'protocol.json': {'source': {'hash': digest(files)}},
                    'baseline-skill.json': {'payload': payload, 'hash': digest(payload)},
                    'candidate-skill-draft.json': {'payload': candidate, 'sha256': digest(candidate)},
                    'development-tasks.json': [dict(id=key, title='Fixture task', allowed_changes=['log_inspector/__main__.py', 'README.md', 'new tests'],
                         acceptance=['Functional requirement', 'Existing tests', 'Independent evaluator isolated tests for BOTH arms', 'No outside access']) for key in ('dev-01', 'dev-05')]}
            for name, value in docs.items(): (preparation / name).write_text(json.dumps(value), encoding='utf-8')
            # No heldout file is created: successful preparation proves it is not read.
            report = root / 'calibration.json'; report.write_text(json.dumps(self.calibration), encoding='utf-8')
            output = root / 'pilot'; receipt = prepare(preparation, output, report)
            self.assertEqual(receipt['model_calls'], 0)
            self.assertFalse(receipt['candidate']['active'])
            store = Store(receipt['database'])
            try:
                preview = TaskEvaluation(store, 'pilot', 'verification-workflow').preview()['evaluation_preview']
                self.assertEqual(len(preview['pairs']), 2)
                for pair in preview['pairs']:
                    state = pair['evaluation_pair_preview']
                    self.assertEqual(state['status'], 'ready'); self.assertEqual(state['memories'], [])
                    self.assertNotIn('Independent evaluator isolated', state['task'])
                    if state['case_id'] == 'dev-01':
                        self.assertIn('仅在 --aggregate 聚合模式下', state['task'])
                        self.assertIn('非聚合模式必须保留“输出 N 条”', state['task'])
                        self.assertIn('不得写成“输出 N 组”', state['task'])
                    self.assertEqual(state['results'], {})
                    self.assertEqual(state['baseline']['hash'], digest(payload))
                    self.assertEqual(state['candidate']['hash'], digest(candidate))
                    self.assertEqual(state['source_hash'], digest(files))
            finally: store.close()
            with self.assertRaises(ValueError): prepare(preparation, output, report)
            docs['candidate-skill-draft.json']['sha256'] = 'tampered'
            (preparation / 'candidate-skill-draft.json').write_text(json.dumps(docs['candidate-skill-draft.json']), encoding='utf-8')
            with self.assertRaises(ValueError): prepare(preparation, root / 'another-pilot', report)

    def test_wording_fix_is_local_to_count_task_and_does_not_leak_grader_policy(self):
        task = dict(id='dev-01', title='修正聚合计数', allowed_changes=['log_inspector/__main__.py'],
                    acceptance=['Preserve behavior', 'Original tests', 'Independent isolated grading', 'No outside access'])
        prompt = task_prompt(task)
        self.assertIn('非聚合模式必须保留“输出 N 条”', prompt)
        self.assertIn('其他诊断语义、stdout内容和退出码保持不变', prompt)
        self.assertNotIn('Independent isolated grading', prompt)
        other = task_prompt(task | {'id': 'dev-05'})
        self.assertNotIn('输出 N 条', other)


if __name__ == '__main__': unittest.main()
