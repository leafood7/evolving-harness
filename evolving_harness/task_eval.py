"""Configurable, evidence-preserving paired Python task evaluation.

Evaluator code is trusted user configuration, frozen before model execution.
It runs with current-user permissions; neither copies nor argv are an OS sandbox.
"""
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import sys
import tempfile
import time

from .components import Components
from .domain import digest, uid
from .evidence import snapshot
from .memory import Memory, select_memories
from .personalization import budget_memories
from .runtime import redact
from .skill_compare import SkillCompare, EXCLUDE, SUFFIXES, linked


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Use relative POSIX paths in evaluation configuration')
    p = PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or value.startswith('/'):
        raise ValueError('Evaluation path escapes workspace')
    return value


def inside(root, value):
    root = Path(root).resolve()
    relative(value)
    p = root / value
    current = root
    for part in PurePosixPath(value).parts:
        current = current / part
        if linked(current):
            raise ValueError('Evaluation paths cannot use links or junctions')
    if not p.resolve().is_relative_to(root):
        raise ValueError('Evaluation path escapes workspace')
    return p.resolve()


def collect(root, excluded=()):
    root = Path(root).resolve()
    if linked(root) or not root.is_dir():
        raise ValueError('Expected an existing source directory')
    files = {}; total = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDE | {'task-evals', 'skill-comparisons', 'task-cards'} and not d.startswith('.')]
        for name in dirs + names:
            if linked(Path(base) / name):
                raise ValueError('Source links and junctions are not supported')
        for name in sorted(names):
            p = Path(base) / name
            if p.relative_to(root).as_posix() in excluded:
                continue
            if name.startswith('.') or p.suffix.lower() not in SUFFIXES:
                continue
            if len(files) >= 80 or p.stat().st_size > 128000:
                raise ValueError('Source exceeds 80 files or 128KB per file')
            text = p.read_text(encoding='utf-8'); total += len(text.encode('utf-8'))
            if total > 1000000:
                raise ValueError('Source exceeds 1MB')
            files[p.relative_to(root).as_posix()] = text
    if not files:
        raise ValueError('Source must contain supported text files')
    return files


def validate(config):
    if not isinstance(config, dict) or set(config) != {'version', 'name', 'baseline', 'candidate', 'cases'}:
        raise ValueError('Invalid evaluation suite fields')
    if type(config['version']) is not int or config['version'] != 1:
        raise ValueError('Unsupported evaluation suite version')
    if not isinstance(config['name'], str) or not 1 <= len(config['name']) <= 100:
        raise ValueError('Suite name required, at most 100 characters')
    for name in ('baseline', 'candidate'):
        if config[name] is None and name == 'baseline':
            continue
        if not isinstance(config[name], str) or not 1 <= len(config[name]) <= 100:
            raise ValueError('Skill component ID or active skill:name required')
    cases = config['cases']
    if not isinstance(cases, list) or not 1 <= len(cases) <= 5:
        raise ValueError('A suite needs 1 to 5 cases')
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'id', 'source', 'prompt', 'protected', 'checks'}:
            raise ValueError('Invalid case fields')
        if not isinstance(case['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', case['id']) or case['id'] in ids:
            raise ValueError('Case IDs must be unique simple names')
        ids.add(case['id']); relative(case['source'])
        if not isinstance(case['prompt'], str) or not 1 <= len(case['prompt']) <= 6000:
            raise ValueError('Case prompt required, at most 6000 characters')
        if not isinstance(case['protected'], list) or len(case['protected']) > 30:
            raise ValueError('Invalid protected paths')
        for path in case['protected']:
            relative(path)
        checks = case['checks']
        if not isinstance(checks, list) or not 1 <= len(checks) <= 3:
            raise ValueError('A case needs 1 to 3 trusted checks')
        for check in checks:
            if not isinstance(check, dict) or set(check) != {'name', 'args', 'require_tests'}:
                raise ValueError('Check requires name, Python args and require_tests')
            if not isinstance(check['name'], str) or not 1 <= len(check['name']) <= 100:
                raise ValueError('Check name required')
            args = check['args']
            if not isinstance(args, list) or not 1 <= len(args) <= 20 or not all(isinstance(a, str) and len(a) <= 8000 for a in args):
                raise ValueError('Invalid Python argument list')
            if type(check['require_tests']) is not bool:
                raise ValueError('require_tests must be boolean')
        if not any(c['require_tests'] for c in checks):
            raise ValueError('At least one check must require a nonzero unittest count')
    return config


def integrity(root, state):
    """Explain failed integrity checks without including source or changing it."""
    violations = []
    for name in state['protected']:
        try:
            p = inside(root, name)
            if not p.is_file():
                violations.append({'path': name, 'reason': 'missing', 'protected': True})
            elif p.stat().st_size > 128000:
                violations.append({'path': name, 'reason': 'oversized', 'protected': True})
            else:
                text = p.read_text(encoding='utf-8')
                if text != state['files'][name]:
                    violations.append({'path': name, 'reason': 'changed', 'protected': True,
                                       'expected_hash': digest(state['files'][name]), 'actual_hash': digest(text)})
        except (ValueError, OSError, UnicodeError) as exc:
            violations.append({'path': name, 'reason': 'unreadable_or_unsafe', 'protected': True,
                               'detail': redact(str(exc))[:1000]})
    try:
        # Also reject new links and source-limit violations; don't mislabel them
        # as modifications to an otherwise unchanged protected file.
        captured = collect(root, state.get('excluded', []))
        if state.get('test_discovery_policy') == 'identifier-test-files-v1':
            for name in captured:
                path = PurePosixPath(name)
                if (name not in state['files'] and name.startswith('tests/') and path.suffix == '.py'
                        and path.name.startswith('test') and not path.stem.isidentifier()):
                    violations.append({'path': name, 'reason': 'undiscoverable_test_filename', 'protected': False,
                                       'detail': 'unittest discovery skips non-identifier module names; use underscores.'})
    except (ValueError, OSError, UnicodeError) as exc:
        violations.append({'path': '.', 'reason': 'workspace_invalid', 'protected': False,
                           'detail': redact(str(exc))[:1000]})
    return {'integrity_ok': not violations,
            'protected_intact': not any(v['protected'] for v in violations), 'violations': violations}


def intact(root, state):
    return integrity(root, state)['integrity_ok']


def reference(root, state, max_seconds=None):
    root = Path(root)
    initial = integrity(root, state)
    if not initial['integrity_ok']:
        return {'status': 'protected_changed' if not initial['protected_intact'] else 'workspace_invalid', **initial}
    before = snapshot(str(root)); outputs = []; valid = True; counts = []
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None
    env = os.environ.copy(); env.pop('PYTHONPATH', None); env['PYTHONUTF8'] = '1'
    for check in state['checks']:
        remaining = min(10, deadline - time.monotonic()) if deadline is not None else 10
        if remaining <= 0:
            return {'status': 'timeout', 'budget_exhausted': True, 'checks': outputs, **integrity(root, state)}
        args = [sys.executable, *check['args']]
        try:
            with tempfile.TemporaryFile() as capture:
                child = subprocess.Popen(args, cwd=root, env=env, stdout=capture, stderr=subprocess.STDOUT)
                try:
                    child.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
                    return {'status': 'timeout', 'checks': outputs, **integrity(root, state)}
                capture.seek(0); output = capture.read(24000).decode('utf-8', errors='replace')
            match = re.search(r'Ran (\d+) tests?\b', output)
            count = int(match[1]) if match else 0
            passed = child.returncode == 0 and (not check['require_tests'] or count > 0)
            outputs.append({'name': check['name'], 'command': args, 'exit_code': child.returncode,
                            'output': redact(output), 'test_count': count, 'passed': passed})
            counts.append(count); valid = valid and passed
        except OSError as exc:
            return {'status': 'failed', 'error': redact(str(exc)), 'checks': outputs}
    final = integrity(root, state)
    stable = before['manifest_hash'] == snapshot(str(root))['manifest_hash']
    status = ('protected_changed' if not final['protected_intact'] else 'workspace_invalid'
              if not final['integrity_ok'] else 'passed' if valid and stable else 'failed')
    return {'status': status,
            'checks': outputs, 'test_count': max(counts, default=0),
            **final, 'snapshot_stable': stable}


def assert_promotion_allowed(store, owner, project, candidate_id):
    """Enrolled task evaluations add a gate; failures cannot be hidden by a new suite."""
    for event in store.events(owner, project, 'task_eval_suite'):
        suite = event['data']
        if suite['candidate']['id'] != candidate_id:
            continue
        models = set()
        for pair_id in suite['pairs']:
            state = TaskPair(store, owner, project, pair_id).current()
            if state['status'] != 'completed':
                raise ValueError('Candidate task evaluation is incomplete; finish it before activation')
            if state.get('comparison', {}).get('outcome') not in {'tie_on_this_task', 'improved_on_this_task'}:
                raise ValueError('Candidate has a failed, regressed or inconclusive task evaluation; create a corrected version')
            models.add(digest(state.get('model')))
        if len(models) > 1:
            raise ValueError('Candidate task evaluation mixed models across cases')


class TaskPair(SkillCompare):
    event_kind = 'task_eval_pair'
    preview_key = 'evaluation_pair_preview'
    result_key = 'evaluation_pair_result'
    interface = 'task-evaluation'

    def __init__(self, store, owner, project, pair_id):
        super().__init__(store, owner, project); self.pair_id = pair_id

    def current(self):
        events = self.store.events(self.owner, self.project, self.event_kind)
        for event in reversed(events):
            if event['data']['id'] == self.pair_id:
                return event['data']
        raise ValueError('Evaluation pair not found in current scope')

    def collect(self, root): return collect(root, self.current().get('excluded', []))
    def verify(self, root, state):
        control = getattr(self, 'control', None)
        return reference(root, state, control.remaining_seconds() if control else None)
    def workflow(self, name, state):
        return state['candidate'] if name == 'with_skill' else state['baseline']


class TaskEvaluation:
    def __init__(self, store, owner, project, frozen_context=None):
        self.store, self.owner, self.project = store, owner, project
        self.frozen_context = frozen_context

    def current(self, suite_id=None):
        for event in reversed(self.store.events(self.owner, self.project, 'task_eval_suite')):
            if suite_id is None or event['data']['id'] == suite_id:
                return event['data']
        raise ValueError('Prepare an evaluation suite first in this user/project')

    def prepare(self, workspace, manifest):
        root = Path(workspace).resolve(); path = inside(root, manifest)
        if not path.is_file() or path.stat().st_size > 64000:
            raise ValueError('Evaluation manifest must be an existing JSON file up to 64KB')
        config = validate(json.loads(path.read_text(encoding='utf-8-sig')))
        registry = Components(self.store, self.owner, self.project)
        def skill(ref):
            c = registry.active(ref) if ref.startswith('skill:') else registry.get(ref)
            if not c or c['payload']['kind'] != 'skill':
                raise ValueError('Evaluation supports scoped Skill components only')
            return c
        baseline = skill(config['baseline']) if config['baseline'] else None
        candidate = skill(config['candidate'])
        if baseline and baseline['hash'] == candidate['hash']:
            raise ValueError('Baseline and candidate Skill payloads are identical')
        coding = self.store.current(self.owner, self.project, 'coding')['payload']['instructions']
        profile = Components(self.store, self.owner, '@user').seed('profile')['payload']
        context = registry.seed('context')['payload']
        policy = self.store.current(self.owner, self.project, 'memory-policy')['payload']
        if self.frozen_context is not None:
            coding = self.frozen_context['coding']
            profile = self.frozen_context['profile']
        # Validate every source before creating any state or workspaces.
        cases = []
        for case in config['cases']:
            source = inside(path.parent, case['source'])
            if not source.is_relative_to(root):
                raise ValueError('Case source must stay inside selected workspace')
            excluded = [path.relative_to(source).as_posix()] if path.is_relative_to(source) else []
            files = collect(source, excluded)
            check_scripts = {arg for check in case['checks'] for arg in check['args'] if arg in files and arg.endswith('.py')}
            protected = sorted({name for name in files if name == 'AGENTS.md' or name.startswith('tests/')} |
                               set(case['protected']) | check_scripts)
            if any(name not in files for name in protected):
                raise ValueError('Protected paths must name existing captured text files')
            cases.append((case, source, files, protected, excluded))
        suite_id = uid('eval'); pairs = []
        parent = Path(self.store.db.execute('PRAGMA database_list').fetchone()[2]).parent / 'task-evals' / suite_id
        for case, source, files, protected, excluded in cases:
            pair_id = uid('pair'); arms = {}
            for name in ('without_skill', 'with_skill'):
                target = parent / pair_id / ('workspace-' + secrets.token_hex(4)); target.mkdir(parents=True)
                for name_, text in files.items():
                    p = target / name_; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding='utf-8')
                arms[name] = {'workspace': str(target), 'status': 'ready'}
            if self.frozen_context is not None:
                memories = self.frozen_context['memories']
            else:
                memories = select_memories(Memory(self.store).list(self.owner, self.project), policy,
                                           owner=self.owner, project=self.project, query=case['prompt'])
                memories = budget_memories(memories, context)
            from .native import native_tools
            available_tools = native_tools(True)
            constraints = '\nModify only this workspace copy. Do not change these existing protected files: ' + ', '.join(protected)
            constraints += '\nThis source-only copy has no Git metadata: do not assume git status/diff works or claim a clean git diff. Track the files you edit and check their contents against what you read before editing. Available tool names: ' + available_tools + '.'
            constraints += '\nAll existing tests/ files are read-only: do not edit, append tests to, rename or delete them. Add regression tests only in NEW files, e.g. tests/test_task_' + case['id'].replace('-', '_') + '.py. New test module names must be valid Python identifiers; use underscores, not hyphens. Adding assertions to an existing protected test file is also forbidden.'
            constraints += '\nDo not install dependencies, access the network or read other directories. Run project tests and report actual results.'
            state = {'id': pair_id, 'suite_id': suite_id, 'case_id': case['id'], 'status': 'ready',
                     'source': str(source), 'source_hash': digest(files), 'files': files,
                     'excluded': excluded,
                     'task': case['prompt'] + constraints, 'checks': case['checks'], 'protected': protected,
                     'test_discovery_policy': 'identifier-test-files-v1',
                     'available_tools': available_tools,
                     'baseline': baseline, 'candidate': candidate, 'coding': coding, 'profile': profile,
                     'memories': [{k: m[k] for k in ('id', 'revision', 'content')} for m in memories],
                     'arms': arms, 'order': secrets.SystemRandom().sample(list(arms), 2), 'results': {}}
            TaskPair(self.store, self.owner, self.project, pair_id).save(state)
            pairs.append(pair_id)
        suite = {'id': suite_id, 'name': config['name'], 'manifest_hash': digest(config), 'pairs': pairs,
                 'baseline': baseline, 'candidate': candidate,
                 'backend': 'real-pi-rpc-on-confirmation',
                 'notice': 'Trusted Python evaluators run with user permissions. No OS sandbox, no automatic promotion. Checks are frozen, not supplied as scoring hints to the model. Small suites do not prove general gains.'}
        self.store.event(self.owner, self.project, 'task_eval_suite', suite)
        return self.preview(suite_id)

    def preview(self, suite_id=None):
        suite = self.current(suite_id)
        pairs = [TaskPair(self.store, self.owner, self.project, key).preview() for key in suite['pairs']]
        return {'evaluation_preview': suite | {'pairs': pairs}, 'packet_hash': digest(suite)}

    async def run_case(self, suite_id, pair_id, packet_hash, pi_root, provider, model):
        suite = self.current(suite_id)
        if pair_id not in suite['pairs']:
            raise ValueError('Pair is outside selected suite')
        pair = TaskPair(self.store, self.owner, self.project, pair_id)
        preview = pair.preview()
        if preview['evaluation_pair_preview'].get('batch_managed'):
            raise ValueError('Use the batch controller for this managed pair and its limits')
        if preview['packet_hash'] != packet_hash:
            raise ValueError('Evaluation preview changed or already attempted')
        await pair.run(preview, pi_root, provider, model)
        return self.report(suite_id)

    def report(self, suite_id=None):
        suite = self.current(suite_id); rows = []; counts = {}
        for key in suite['pairs']:
            state = TaskPair(self.store, self.owner, self.project, key).current()
            outcome = state.get('comparison', {}).get('outcome', state['status'])
            counts[outcome] = counts.get(outcome, 0) + 1
            arms = {}
            for name, result in state['results'].items():
                arms['baseline' if name == 'without_skill' else 'candidate'] = {
                    'task_id': result['task_id'], 'status': result['status'], 'error': result['error'],
                    'execution_limit': result.get('execution_limit'),
                    'reference': result['reference'], 'metrics': result['metrics'], 'answer': result['text']}
            rows.append({'case_id': state['case_id'], 'pair_id': key, 'outcome': outcome,
                         'model': state.get('model'),
                         'source_hash': state['source_hash'], 'original_source_unchanged': state.get('original_source_unchanged'),
                         'checks_hash': digest(state['checks']), 'arms': arms})
        done = all(r['outcome'] not in {'ready', 'running'} for r in rows)
        outcomes = set(counts)
        models = {digest(r['model']) for r in rows if r['model']}
        recommendation = ('pending' if not done else 'do_not_promote' if 'worse_on_this_task' in outcomes
                          else 'insufficient_evidence' if outcomes & {'inconclusive', 'both_failed'}
                          else 'review_candidate' if 'improved_on_this_task' in outcomes else 'keep_current')
        if done and len(models) > 1 and recommendation != 'do_not_promote':
            recommendation = 'insufficient_evidence'
        summary = {}
        for arm in ('baseline', 'candidate'):
            results = [r['arms'][arm] for r in rows if arm in r['arms']]
            known_costs = [r['metrics']['reported_cost'] for r in results if r['metrics']['reported_cost'] is not None]
            summary[arm] = {'attempted': len(results),
                'independent_passed': sum(r['reference']['status'] == 'passed' for r in results),
                'tool_calls': sum(r['metrics']['tool_calls'] for r in results),
                'tool_errors': sum(r['metrics']['tool_errors'] for r in results),
                'elapsed_seconds': round(sum(r['metrics']['elapsed_seconds'] for r in results), 2),
                'known_reported_cost': sum(known_costs) if known_costs else None,
                'missing_cost_runs': len(results) - len(known_costs)}
        report = {'id': suite['id'], 'name': suite['name'], 'manifest_hash': suite['manifest_hash'],
                  'baseline': suite['baseline'], 'candidate': suite['candidate'], 'complete': done,
                  'counts': counts, 'summary': summary, 'recommendation': recommendation, 'cases': rows,
                  'consistent_model_across_cases': len(models) <= 1,
                  'notice': suite['notice'], 'automatic_activation': False}
        directory = Path(self.store.db.execute('PRAGMA database_list').fetchone()[2]).parent / 'task-evals' / suite['id']
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        body = '<!doctype html><html lang="zh"><meta charset="utf-8"><title>Harness evaluation</title>'
        body += '<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
        labels = {'pending': '待完成', 'do_not_promote': '拒绝晋升', 'insufficient_evidence': '证据不足',
                  'review_candidate': '候选有改善，待人工复核', 'keep_current': '保留当前，尚未证明收益'}
        body += '<h1>' + html.escape(suite['name']) + '</h1><p>' + html.escape(labels[recommendation]) + '</p>'
        body += '<p>本报告不会自动启用候选。费用缺失不当成零，平局和失败完整保留。</p>'
        body += '<h2>汇总</h2><table><tr><th>组别</th><th>已尝试</th><th>独立通过</th><th>工具调用</th><th>工具错误</th><th>整组累计秒数</th><th>已知报告费用 / 缺失次数</th></tr>'
        for name, values in summary.items():
            fields = ['基线' if name == 'baseline' else '候选', values['attempted'], values['independent_passed'],
                      values['tool_calls'], values['tool_errors'], values['elapsed_seconds'],
                      str(values['known_reported_cost']) + ' / ' + str(values['missing_cost_runs'])]
            body += '<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in fields) + '</tr>'
        body += '</table><h2>逐任务结果</h2>'
        body += '<p>' + html.escape(suite['notice']) + '</p><table><tr><th>Case</th><th>Outcome</th><th>Baseline / candidate checks</th><th>违规文件或完整性问题</th></tr>'
        for row in rows:
            checks = ' / '.join(row['arms'].get(a, {}).get('reference', {}).get('status', 'pending') for a in ('baseline', 'candidate'))
            reasons = '; '.join(arm + ': ' + v['path'] + ' (' + v['reason'] + ')'
                                for arm, result in row['arms'].items() for v in result['reference'].get('violations', []))
            body += '<tr>' + ''.join('<td>' + html.escape(str(x)) + '</td>' for x in (row['case_id'], row['outcome'], checks, reasons or '—')) + '</tr>'
        body += '</table><details><summary>Evidence and metrics</summary><pre>' + html.escape(json.dumps(report, ensure_ascii=False, indent=2)) + '</pre></details></html>'
        (directory / 'report.html').write_text(body, encoding='utf-8')
        return {'evaluation_report': report, 'report_json': str(directory / 'report.json'), 'report_html': str(directory / 'report.html')}
