"""Prepare two development cases in a fresh DB. Never execute or activate C1."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.log_inspector.checks import probe_source
from evolving_harness.bootstrap import bootstrap
from evolving_harness.components import Components
from evolving_harness.domain import digest
from evolving_harness.store import Store
from evolving_harness.task_eval import TaskEvaluation, collect

PILOT_IDS = ('dev-01', 'dev-05')
PROMPT_REVISION = 'diagnostic-units-explicit-v2'


def task_prompt(task):
    """State user-visible behavior explicitly; keep evaluator hints out."""
    detail = ('仅在 --aggregate 聚合模式下，使用“匹配 N 条”和“输出 N 组”分别报告匹配日志数与实际输出聚合组数；'
              '--top 截断后按实际输出组数计数，无匹配时输出 0 组。'
              '非聚合模式必须保留“输出 N 条”，不得写成“输出 N 组”；可增加匹配数，但其他诊断语义、stdout内容和退出码保持不变。'
              if task['id'] == 'dev-01' else '')
    # Final three acceptance entries belong to evaluator/isolation policy,
    # not the actor task. They must not force extra verification in both arms.
    prompt = task['title'] + '。' + detail + '\n' + '\n'.join(task['acceptance'][:-3])
    return prompt + '\n允许修改：' + ', '.join(task['allowed_changes']) + '。修改后增加回归测试并报告实际运行结果。'


def prepare(preparation, output, calibration):
    preparation = Path(preparation).resolve(); output = Path(output).resolve()
    if output.exists(): raise ValueError('Choose a new pilot directory; never overwrite history')
    protocol = json.loads((preparation / 'protocol.json').read_text(encoding='utf-8'))
    calibration = json.loads(Path(calibration).read_text(encoding='utf-8'))
    rows = {r['task_id']: r for r in calibration['tasks']}
    if not calibration.get('passed') or not calibration.get('source_unchanged'):
        raise ValueError('Passing calibration evidence required')
    # The pilot cannot load held-out tasks, even if their graders have been calibrated.
    tasks = {t['id']: t for t in json.loads((preparation / 'development-tasks.json').read_text(encoding='utf-8'))}
    files = collect(preparation / 'frozen-source')
    if digest(files) != protocol['source']['hash']:
        raise ValueError('Frozen source hash changed')
    baseline = json.loads((preparation / 'baseline-skill.json').read_text(encoding='utf-8'))
    candidate = json.loads((preparation / 'candidate-skill-draft.json').read_text(encoding='utf-8'))
    if digest(baseline['payload']) != baseline['hash'] or digest(candidate['payload']) != candidate['sha256']:
        raise ValueError('Workflow payload hash changed')
    for key in PILOT_IDS:
        if key not in tasks or key not in rows or not rows[key]['validated']:
            raise ValueError('Pilot development task is missing validated checks')
        if hashlib.sha256(probe_source(key).encode()).hexdigest() != rows[key]['probe_sha256']:
            raise ValueError('Checker changed after calibration; rerun calibration first')
    output.mkdir(parents=True)
    shutil.copytree(preparation / 'frozen-source', output / 'source', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    db = output / '.harness/state.sqlite3'; store = Store(db)
    owner, project = 'pilot', 'verification-workflow'
    try:
        bootstrap(store, owner, project)
        registry = Components(store, owner, project)
        old = registry.propose('skill:portable-test-entry', baseline['payload'], [], seed=True,
            backend='frozen-manual-baseline', reason='Exact copy of frozen v1; separate experimental DB')
        new = registry.propose('skill:portable-test-entry', candidate['payload'], [],
            backend='manual-evidence-review', reason='Clarify conditional verification only; no autonomous evolution claim')
        cases = []
        for key in PILOT_IDS:
            task = tasks[key]
            allowed = set(p for p in task['allowed_changes'] if p != 'new tests')
            protected = sorted(p for p in files if p not in allowed)
            prompt = task_prompt(task)
            cases.append({'id': key, 'source': 'source', 'prompt': prompt, 'protected': protected,
                          'checks': [{'name': 'full-isolated-regression', 'args': ['-I', '-X', 'utf8', 'checks/run_tests.py'], 'require_tests': True},
                                     {'name': 'frozen-independent-contract', 'args': ['-X', 'utf8', '-c', probe_source(key)], 'require_tests': False}]})
        config = {'version': 1, 'name': 'Verification workflow pilot — development only',
                  'baseline': old['id'], 'candidate': new['id'], 'cases': cases}
        (output / 'pilot.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        evaluation = TaskEvaluation(store, owner, project)
        preview = evaluation.prepare(output, 'pilot.json')
        for pair in preview['evaluation_preview']['pairs']:
            state = pair['evaluation_pair_preview']
            assert state['memories'] == [] and state['profile']['preferences'] == []
            assert state['source_hash'] == protocol['source']['hash']
        receipt = {'status': 'PREPARED_NO_MODEL_RUNS', 'model_calls': 0, 'database': str(db),
                   'owner': owner, 'project': project, 'workspace': str(output),
                   'suite_id': preview['evaluation_preview']['id'], 'case_ids': list(PILOT_IDS),
                   'prompt_revision': PROMPT_REVISION,
                   'baseline': {'id': old['id'], 'hash': old['hash']},
                   'candidate': {'id': new['id'], 'hash': new['hash'], 'active': False},
                   'source_hash': protocol['source']['hash'], 'empty_memory_and_profile': True,
                   'new_sessions_and_randomized_arm_order': True,
                   'pilot_only_not_formal_repeat_study': True,
                   'live_requirements': 'Review full payload, agree money/time budget, then confirm four task runs. No execution was authorized or made by this preparer.',
                   'scope_notice': 'This legacy teaching pilot is not a repeated study. Use the implemented batch controller for repeat indices, split isolation, budgets and intervention records; no automatic live runs.'}
        (output / 'prepared.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        evaluation.report()
        return receipt
    finally: store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preparation', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--calibration', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.preparation, args.output, args.calibration), ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
