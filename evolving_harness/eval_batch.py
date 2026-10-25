"""Frozen, repeated paired evaluations. Local isolation is not an OS sandbox."""
import html
import json
import math
from pathlib import Path
import re
import sys
import time

from .bootstrap import bootstrap
from .components import Components
from .domain import canonical, digest, now, uid
from .rpc import pi_command
from .store import Store
from .skill_compare import MODEL_TIMEOUT, ARM_MODEL_TIMEOUT
from .task_eval import TaskEvaluation, TaskPair, collect, inside, validate


def runtime(pi_root):
    command = pi_command(pi_root)
    root = Path(__file__).resolve().parent
    import hashlib
    paths = list(root.glob('*.py')) + [root.parent / 'extensions/evolving.ts', Path(command[1])]
    return {'python': sys.version, 'executable': sys.executable, 'node': command[0],
            'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}


def configuration(value):
    fields = {'version', 'name', 'baseline', 'candidate', 'model', 'repeats', 'limits', 'development', 'heldout'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Invalid batch fields')
    if type(value['repeats']) is not int or not 1 <= value['repeats'] <= 3:
        raise ValueError('Repeat count must be 1 to 3')
    model = value['model']
    if not isinstance(model, dict) or set(model) != {'provider', 'model'} or any(
            not isinstance(v, str) or not 1 <= len(v) <= 100 for v in model.values()):
        raise ValueError('Freeze provider and model before preparation')
    limits = value['limits']
    ranges = {'max_arm_runs': (2, 90), 'max_seconds': (10, 14400),
              'max_tokens': (1, 100000000), 'max_model_messages': (1, 10000)}
    required = set(ranges) | {'max_estimated_cost'}
    if not isinstance(limits, dict) or not required <= set(limits) or set(limits) - required - {'max_arm_model_messages'}:
        raise ValueError('Specify all batch limits')
    for key, (low, high) in ranges.items():
        if key in {'max_tokens', 'max_model_messages'} and limits[key] is None:
            continue
        if type(limits[key]) is not int or not low <= limits[key] <= high:
            raise ValueError('Invalid limit: ' + key)
    arm_messages = limits.get('max_arm_model_messages')
    if arm_messages is not None and (type(arm_messages) is not int or not 1 <= arm_messages <= 1000):
        raise ValueError('Invalid per-arm model message limit')
    cost = limits['max_estimated_cost']
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost <= 0:
        raise ValueError('Estimated cost limit must be finite and positive')
    ids = set()
    for split, low, high in [('development', 1, 10), ('heldout', 0, 5)]:
        cases = value[split]
        if not isinstance(cases, list) or not low <= len(cases) <= high:
            raise ValueError('Invalid split size: ' + split)
        for start in range(0, len(cases), 5):
            validate({k: value[k] for k in ('version', 'name', 'baseline', 'candidate')} |
                     {'cases': cases[start:start + 5]})
        for case in cases:
            if case['id'] in ids:
                raise ValueError('Case IDs must be unique across splits')
            ids.add(case['id'])
    return value


def execution_policy(state):
    limits = state['limits']
    planned = 2 * (len(state['entries']) + (0 if state['heldout_unlocked'] else state['heldout_count'] * state['repeats']))
    arm_cap = limits.get('max_arm_model_messages')
    total_cap = limits['max_model_messages']
    warnings = []
    if total_cap is not None:
        warnings.append('max_model_messages is shared by ALL arms; it can interrupt an otherwise successful pair.')
    if arm_cap is not None and total_cap is not None and total_cap < planned * arm_cap:
        warnings.append('The batch message allowance is smaller than planned arms times the per-arm ceiling; not all arms can use their full allowance.')
    return {'planned_arms': planned, 'batch_limits': limits,
            'per_arm': {'model_prompt_timeout_seconds': MODEL_TIMEOUT,
                        'model_scope_timeout_seconds': ARM_MODEL_TIMEOUT,
                        'max_model_messages': arm_cap},
            'warnings': warnings,
            'behavior': 'Per-arm message cutoff records a failed/inconclusive arm and continues without retry when usage is known. Aggregate limits or incomplete billing usage stop later arms. Timeouts are labeled separately; unknown in-flight usage still stops the batch.'}


class Budget:
    def __init__(self, batch, state, entry):
        self.batch, self.state, self.entry = batch, state, entry
        self.started = time.monotonic(); self.base = state['usage']['seconds']
        self.stop_reason = None; self.messages = 0; self.arm_stop_reason = None

    def remaining_seconds(self):
        return max(0, self.state['limits']['max_seconds'] - self.base - (time.monotonic() - self.started))

    def persist(self):
        self.state['usage']['seconds'] = self.base + time.monotonic() - self.started
        self.batch.save(self.state)

    def stop(self, reason):
        self.stop_reason = reason
        self.state['stop_reason'] = reason
        self.persist()

    def before_arm(self, name):
        if self.remaining_seconds() <= 0:
            self.stop('time_limit'); return False
        if self.batch.current(self.state['id']).get('stop_requested'):
            self.stop('user_stop'); return False
        self.messages = 0; self.arm_stop_reason = None
        self.state['usage']['arm_runs'] += 1
        self.entry['attempted_arms'].append(name)
        self.persist()
        return True

    def observe(self, event):
        message = event.get('message') or {}
        if message.get('role') != 'assistant':
            return
        self.messages += 1
        totals = self.state['usage']; totals['model_messages'] += 1
        usage = message.get('usage') or {}; cost = (usage.get('cost') or {}).get('total')
        tokens = usage.get('totalTokens')
        valid = lambda x: type(x) in (int, float) and math.isfinite(x) and x >= 0
        if not valid(tokens) or not valid(cost):
            totals['missing_usage_messages'] += 1
            self.stop('missing_usage'); raise RuntimeError('Batch stopped: missing usage or estimated cost')
        totals['tokens'] += tokens; totals['estimated_cost'] += cost
        self.persist()
        for key, limit in [('tokens', 'max_tokens'), ('estimated_cost', 'max_estimated_cost'),
                           ('model_messages', 'max_model_messages')]:
            if self.state['limits'][limit] is not None and totals[key] >= self.state['limits'][limit]:
                self.stop(limit); raise RuntimeError('Batch stopped: ' + limit)
        if self.remaining_seconds() <= 0:
            self.stop('time_limit'); raise RuntimeError('Batch time limit')
        arm_limit = self.state['limits'].get('max_arm_model_messages')
        if arm_limit is not None and self.messages >= arm_limit:
            self.arm_stop_reason = 'arm_model_messages'
            raise RuntimeError('Arm stopped: model message limit; no retry')

    def after_arm(self, result):
        if self.arm_stop_reason:
            result['execution_limit'] = self.arm_stop_reason
        if self.stop_reason:
            self.persist()
        elif self.remaining_seconds() <= 0:
            self.stop('time_limit')
        elif self.arm_stop_reason and self.messages:
            # The usage-bearing event that triggered the local cutoff was recorded.
            # Keep the failed arm; don't turn its passing code check into success.
            self.persist()
        elif not self.messages or result['error']:
            self.stop(self.stop_reason or 'incomplete_model_usage')
        else:
            self.persist()


class EvaluationBatch:
    kind = 'evaluation_batch'

    def __init__(self, store, owner, project):
        self.store, self.owner, self.project = store, owner, project
        self.base = Path(store.db.execute('PRAGMA database_list').fetchone()[2]).parent / 'eval-batches'

    def current(self, key=None):
        for event in reversed(self.store.events(self.owner, self.project, self.kind)):
            if key is None or event['data']['id'] == key:
                return event['data']
        raise ValueError('Prepare a batch in this user/project first')

    def save(self, state):
        self.store.event(self.owner, self.project, self.kind, state)

    def directory(self, state):
        if not re.fullmatch(r'batch_[a-f0-9]+', state['id']):
            raise ValueError('Invalid batch ID')
        return self.base / state['id']

    def plan(self, state):
        value = json.loads((self.directory(state) / 'plan.json').read_text(encoding='utf-8'))
        if digest(value) != state['plan_hash']:
            raise ValueError('Frozen batch plan changed')
        return value

    def child(self, state, split):
        if split not in {'development', 'heldout'}:
            raise ValueError('Invalid split')
        return Store(self.directory(state) / split / '.harness/state.sqlite3')

    def prepare(self, workspace, manifest, pi_root):
        path = inside(workspace, manifest)
        if not path.is_file() or path.stat().st_size > 512000:
            raise ValueError('Batch manifest must be at most 512KB')
        config = configuration(json.loads(path.read_text(encoding='utf-8-sig')))
        registry = Components(self.store, self.owner, self.project)
        def skill(ref):
            if ref is None: return None
            item = registry.active(ref) if ref.startswith('skill:') else registry.get(ref)
            if not item or item['payload']['kind'] != 'skill': raise ValueError('Scoped Skill required')
            return item
        baseline, candidate = skill(config['baseline']), skill(config['candidate'])
        if baseline and baseline['hash'] == candidate['hash']:
            raise ValueError('Baseline and candidate are identical')
        sources = {}; fingerprints = set(); sealed = []
        for split in ('development', 'heldout'):
            for case in config[split]:
                root = inside(path.parent, case['source'])
                if not root.is_relative_to(Path(workspace).resolve()): raise ValueError('Source outside workspace')
                files = collect(root, [path.relative_to(root).as_posix()] if path.is_relative_to(root) else [])
                if any(name not in files for name in case['protected']):
                    raise ValueError('Protected paths must name captured files')
                key = digest(files); sources[key] = files
                case['source_hash'] = key
                fingerprint = digest({'source': key, 'prompt': case['prompt'], 'checks': case['checks']})
                if fingerprint in fingerprints: raise ValueError('Duplicate tasks across cases or splits')
                fingerprints.add(fingerprint)
                if split == 'heldout': sealed.append(fingerprint)
        if len(canonical(sources).encode('utf-8')) > 5000000:
            raise ValueError('Batch sources exceed 5MB')
        exposed = {f for e in self.store.events(self.owner, self.project, 'heldout_exposure') for f in e['data']['fingerprints']}
        if exposed.intersection(sealed): raise ValueError('Held-out task was already exposed; use a new unseen task')
        frozen = {'config': config, 'sources': sources, 'baseline': baseline, 'candidate': candidate,
                  'coding': self.store.current(self.owner, self.project, 'coding')['payload']['instructions'],
                  'runtime': runtime(pi_root), 'heldout_fingerprints': sealed}
        state = {'id': uid('batch'), 'name': config['name'], 'status': 'prepared', 'split': 'development',
                 'model': config['model'], 'limits': config['limits'], 'repeats': config['repeats'],
                 'candidate_id': candidate['id'], 'plan_hash': digest(frozen), 'entries': [],
                 'heldout_count': len(config['heldout']), 'heldout_unlocked': False,
                 'usage': {'arm_runs': 0, 'seconds': 0, 'tokens': 0, 'estimated_cost': 0,
                           'model_messages': 0, 'missing_usage_messages': 0}}
        directory = self.directory(state); directory.mkdir(parents=True)
        (directory / 'plan.json').write_text(canonical(frozen), encoding='utf-8')
        self.materialize(state, frozen, 'development'); self.save(state)
        return self.preview(state['id'])

    def materialize(self, state, plan, split):
        root = self.directory(state) / split; root.mkdir(parents=True, exist_ok=True)
        child = self.child(state, split)
        try:
            bootstrap(child, self.owner, self.project)
            registry = Components(child, self.owner, self.project)
            refs = {}
            for role in ('baseline', 'candidate'):
                item = plan[role]
                refs[role] = registry.propose('skill:batch-' + role, item['payload'], [], reason='Frozen batch copy')['id'] if item else None
            cases = []
            for original in plan['config'][split]:
                case = {k: v for k, v in original.items() if k != 'source_hash'}
                case['source'] = 'sources/' + case['id']; cases.append(case)
                for name, text in plan['sources'][original['source_hash']].items():
                    target = root / case['source'] / name; target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding='utf-8')
            evaluation = TaskEvaluation(child, self.owner, self.project,
                frozen_context={'coding': plan['coding'], 'profile': {'kind': 'profile', 'preferences': []}, 'memories': []})
            for repeat in range(1, state['repeats'] + 1):
                for start in range(0, len(cases), 5):
                    manifest = root / f'.suite-{repeat}-{start}.json'
                    manifest.write_text(canonical({'version': 1, 'name': state['name'], **refs,
                                                   'cases': cases[start:start + 5]}), encoding='utf-8')
                    result = evaluation.prepare(root, manifest.name)['evaluation_preview']
                    for preview in result['pairs']:
                        pair_state = preview['evaluation_pair_preview']; pair_state['batch_managed'] = state['id']
                        TaskPair(child, self.owner, self.project, pair_state['id']).save(pair_state)
                        state['entries'].append({'id': uid('entry'), 'split': split, 'repeat': repeat,
                            'case_id': pair_state['case_id'], 'suite_id': result['id'], 'pair_id': pair_state['id'],
                            'status': 'ready', 'attempted_arms': []})
        finally:
            child.close()

    def preview(self, key=None):
        state = self.current(key); self.plan(state)
        child = self.child(state, state['split'])
        try:
            pairs = [TaskPair(child, self.owner, self.project, e['pair_id']).preview()
                     for e in state['entries'] if e['split'] == state['split'] and e['status'] == 'ready']
        finally: child.close()
        return {'batch_preview': state | {'payloads': pairs, 'execution_policy': execution_policy(state)}, 'packet_hash': digest(state)}

    def unlock(self, key, packet_hash):
        with self.store.transaction():
            state = self.current(key); plan = self.plan(state)
            if digest(state) != packet_hash or state['status'] != 'development_complete' or not state['heldout_count']:
                raise ValueError('Finish development and confirm its current report before unlocking held-out tasks')
            exposed = {f for e in self.store.events(self.owner, self.project, 'heldout_exposure') for f in e['data']['fingerprints']}
            if exposed.intersection(plan['heldout_fingerprints']): raise ValueError('Held-out tasks already exposed')
            self.materialize(state, plan, 'heldout')
            state.update(split='heldout', status='prepared', heldout_unlocked=True)
            for kind, data in [('heldout_exposure', {'batch_id': key, 'fingerprints': plan['heldout_fingerprints']}), (self.kind, state)]:
                self.store.db.execute('INSERT INTO events(owner,project,kind,data,created) VALUES(?,?,?,?,?)',
                                     (self.owner, self.project, kind, canonical(data), now()))
        return self.preview(key)

    async def run_next(self, key, packet_hash, pi_root, provider, model):
        with self.store.transaction():
            state = self.current(key); plan = self.plan(state)
            if digest(state) != packet_hash or state['status'] not in {'prepared', 'ready'}:
                raise ValueError('Batch preview stale, busy or already stopped; attempted pairs are never replayed')
            if state['model'] != {'provider': provider, 'model': model} or plan['runtime'] != runtime(pi_root):
                raise ValueError('Frozen model or runtime changed; prepare a new experiment')
            entry = next((e for e in state['entries'] if e['split'] == state['split'] and e['status'] == 'ready'), None)
            if not entry: raise ValueError('No pending pair in this phase')
            if state['usage']['arm_runs'] + 2 > state['limits']['max_arm_runs'] or state['usage']['seconds'] >= state['limits']['max_seconds']:
                state.update(status='stopped', stop_reason='run_or_time_limit')
            else:
                entry['status'] = 'running'; state.update(status='running', running_entry=entry['id'], claimed_at=time.time())
            self.store.db.execute('INSERT INTO events(owner,project,kind,data,created) VALUES(?,?,?,?,?)',
                                 (self.owner, self.project, self.kind, canonical(state), now()))
        if state['status'] == 'stopped': return self.report(key)
        child = self.child(state, state['split']); control = Budget(self, state, entry)
        try:
            pair = TaskPair(child, self.owner, self.project, entry['pair_id']); pair.control = control
            await pair.run(pair.preview(), pi_root, provider, model)
            result = pair.current(); entry['status'] = result['status']
            entry['outcome'] = result.get('comparison', {}).get('outcome', 'inconclusive')
            if control.stop_reason: state.update(status='stopped', stop_reason=control.stop_reason)
            elif all(e['status'] != 'ready' for e in state['entries'] if e['split'] == state['split']):
                state['status'] = 'development_complete' if state['split'] == 'development' and state['heldout_count'] else 'completed'
            else: state['status'] = 'ready'
        except BaseException:
            entry['status'] = 'interrupted'; state.update(status='stopped', stop_reason='interrupted_no_retry')
            raise
        finally:
            state.pop('running_entry', None); control.persist(); child.close()
        return self.report(key)

    def note(self, key, entry_id, arm, kind, text):
        state = self.current(key); entry = next((e for e in state['entries'] if e['id'] == entry_id), None)
        if state['status'] == 'running': raise ValueError('Record intervention at a pair boundary')
        if not entry or arm not in {'baseline', 'candidate'} or kind not in {'clarification', 'correction', 'manual_edit', 'takeover'}:
            raise ValueError('Unknown entry, arm or intervention kind')
        if not isinstance(text, str) or not 1 <= len(text) <= 2000: raise ValueError('Bounded intervention note required')
        child = self.child(state, entry['split'])
        try: child.event(self.owner, self.project, 'batch_intervention', {'entry': entry_id, 'arm': arm, 'kind': kind, 'text': text})
        finally: child.close()
        return self.report(key)

    def stop(self, key=None):
        state = self.current(key)
        if state['status'] == 'running': raise ValueError('Wait for the running pair boundary; a crashed pair is marked interrupted after its lease expires')
        state.update(status='stopped', stop_reason='user_stop'); self.save(state)
        return self.report(state['id'])

    def report(self, key=None, *, recover=True):
        state = self.current(key); self.plan(state); rows = []; summaries = {}
        if recover and state['status'] == 'running' and time.time() - state.get('claimed_at', time.time()) > 330:
            for entry in state['entries']:
                if entry['status'] == 'running': entry['status'] = 'interrupted'
            state.update(status='stopped', stop_reason='expired_lease_no_retry')
            state.pop('running_entry', None); self.save(state)
        for split in ('development', 'heldout'):
            entries = [e for e in state['entries'] if e['split'] == split]
            if not entries: continue
            child = self.child(state, split)
            try:
                notes = [e['data'] for e in child.events(self.owner, self.project, 'batch_intervention')]
                for entry in entries:
                    pair = TaskPair(child, self.owner, self.project, entry['pair_id']).current()
                    row = entry | {'arms': {}, 'interventions': [n for n in notes if n['entry'] == entry['id']]}
                    for name, result in pair['results'].items():
                        arm = 'baseline' if name == 'without_skill' else 'candidate'
                        row['arms'][arm] = result
                    rows.append(row)
                for arm in ('baseline', 'candidate'):
                    arm_rows = [r for r in rows if r['split'] == split and arm in r['arms']]
                    passed = [r for r in arm_rows if r['arms'][arm]['reference']['status'] == 'passed']
                    summaries[split + '/' + arm] = {'attempted': len(arm_rows), 'independent_passed': len(passed),
                        'passed_without_declared_help': sum(not any(n['arm'] == arm for n in r['interventions']) for r in passed),
                        'declared_interventions': sum(n['arm'] == arm for r in arm_rows for n in r['interventions'])}
            finally: child.close()
        outcomes = {r.get('outcome') for r in rows}
        decision = ('do_not_promote' if 'worse_on_this_task' in outcomes else
                    'insufficient_evidence' if state['status'] != 'completed' or outcomes & {'both_failed', 'inconclusive', None}
                    or any(r['interventions'] for r in rows) else
                    'review_candidate' if 'improved_on_this_task' in outcomes else 'keep_current')
        report = {'state': state, 'rows': rows, 'summary': summaries, 'recommendation': decision,
                  'execution_policy': execution_policy(state),
                  'automatic_activation': False,
                  'notice': 'Offline preparation is not a model experiment. Fresh sessions/copies; no automatic retries. Held-out separation is logical, not an OS security boundary. Usage and costs are observed estimates, may overshoot thresholds; missing usage stops further runs. Time limits allow process cleanup. Intervention counts are user-declared. Runtime hashes do not freeze remote model weights or all dependencies. Repeats on one project do not prove generalization.'}
        directory = self.directory(state)
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        body = '<!doctype html><meta charset="utf-8"><title>Harness batch</title><h1>批量对照评测</h1>'
        body += '<p>' + html.escape(state['name'] + ' · ' + state['status'] + ' · ' + decision) + '</p>'
        body += '<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:16px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:9px}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
        body += '<p>开发题与预留题分开统计；失败、平局、未完成均保留；不自动启用候选。</p>'
        policy = report['execution_policy']
        body += '<p>单组模型请求超时 ' + str(MODEL_TIMEOUT) + ' 秒，模型作用域超时 ' + str(ARM_MODEL_TIMEOUT) + ' 秒；单组消息上限 ' + str(policy['per_arm']['max_model_messages']) + '。整批限制另计，None 表示未设该项上限。</p>'
        body += ''.join('<p>' + html.escape(w) + '</p>' for w in policy['warnings'])
        body += '<h2>独立验收与人工帮助</h2><table><tr><th>阶段 / 组别</th><th>已尝试</th><th>独立通过</th><th>无已声明帮助通过</th><th>已声明干预</th></tr>'
        for label, values in summaries.items():
            body += '<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in
                [label, values['attempted'], values['independent_passed'], values['passed_without_declared_help'], values['declared_interventions']]) + '</tr>'
        body += '</table><h2>每次重复均保留</h2><table><tr><th>阶段</th><th>任务</th><th>重复</th><th>状态 / 结果</th><th>基线 / 候选验收</th></tr>'
        for row in rows:
            checks = ' / '.join(row['arms'].get(a, {}).get('reference', {}).get('status', '未运行') for a in ('baseline', 'candidate'))
            body += '<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in
                [row['split'], row['case_id'], row['repeat'], row['status'] + ' / ' + str(row.get('outcome', '未完成')), checks]) + '</tr>'
        body += '</table><h2>运行上限与完整证据</h2><p>' + html.escape(report['notice']) + '</p><details><summary>展开 JSON 证据</summary><pre>'
        body += html.escape(json.dumps(report, ensure_ascii=False, indent=2)) + '</pre></details>'
        (directory / 'report.html').write_text(body, encoding='utf-8')
        return {'batch_report': report, 'report_json': str(directory / 'report.json'), 'report_html': str(directory / 'report.html'),
                'packet_hash': digest(state), 'continue_allowed': state['status'] in {'prepared', 'ready'}}


def assert_batch_promotion_allowed(store, owner, project, candidate):
    controller = EvaluationBatch(store, owner, project)
    latest = {e['data']['id']: e['data'] for e in store.events(owner, project, controller.kind)}
    for state in latest.values():
        if state['candidate_id'] == candidate:
            result = controller.report(state['id'], recover=False)['batch_report']
            if state['status'] != 'completed' or result['recommendation'] not in {'review_candidate', 'keep_current'}:
                raise ValueError('Batch is incomplete, stopped, regressed or inconclusive; candidate cannot activate')
