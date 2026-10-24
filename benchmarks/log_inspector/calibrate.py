"""Validate graders offline; never infer model quality from these fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.log_inspector.checks import PROBES, probe_source
from benchmarks.log_inspector.fixtures import apply, MARKER


def fingerprint(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def command(root, args, timeout=20):
    env = os.environ.copy(); env.pop('PYTHONPATH', None); env['PYTHONUTF8'] = '1'
    result = subprocess.run([sys.executable, *args], cwd=root, env=env, capture_output=True,
                            encoding='utf-8', text=True, timeout=timeout)
    return {'exit_code': result.returncode, 'output': (result.stdout + result.stderr)[-16000:]}


def calibrate(source, task_ids=None):
    source = Path(source).resolve(); before = fingerprint(source); rows = []
    with tempfile.TemporaryDirectory(prefix='grader-calibration-') as temporary:
        for task_id in task_ids or PROBES:
            results = {}
            for variant in ('unchanged', 'correct', 'wrong'):
                root = Path(temporary) / task_id / variant
                shutil.copytree(source, root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                (root / MARKER).write_text('Disposable grader calibration fixture. Not an Agent output.\n', encoding='utf-8')
                if variant != 'unchanged': apply(root, task_id, wrong=variant == 'wrong')
                results[variant] = command(root, ['-X', 'utf8', '-c', probe_source(task_id)])
                if variant == 'correct':
                    results['correct_regression'] = command(root, ['-I', '-X', 'utf8', 'checks/run_tests.py'])
            valid = (results['unchanged']['exit_code'] != 0 and results['correct']['exit_code'] == 0
                     and results['wrong']['exit_code'] != 0 and results['correct_regression']['exit_code'] == 0
                     and 'Ran 31 tests' in results['correct_regression']['output'])
            rows.append({'task_id': task_id, 'validated': valid, 'checks': results,
                         'probe_sha256': hashlib.sha256(probe_source(task_id).encode()).hexdigest()})
    stable = before == fingerprint(source)
    return {'backend': 'offline-authored-calibration-fixtures', 'model_calls': 0,
            'source_unchanged': stable, 'passed': stable and all(r['validated'] for r in rows), 'tasks': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).parent / 'source')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists(): raise SystemExit('Choose a new report path; preserve previous results')
    report = calibrate(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'tasks': len(report['tasks']),
                      'source_unchanged': report['source_unchanged'], 'model_calls': 0, 'report': str(args.output)}, ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__': main()
