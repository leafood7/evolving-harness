"""Local evidence preview; no model call, memory mutation or causal claims."""
from collections import Counter

from .memory import Memory


def task_summary(store, owner, project, task_id, memory_id):
    task = store.get_task(owner, project, task_id)
    events = [e for e in store.events(owner, project) if e['task_id'] == task_id]
    contexts = [e for e in events if e['kind'] == 'context_assembled']
    tools = [e for e in events if e['kind'] == 'native_tool' and e['data'].get('phase') == 'start']
    results = [e for e in events if e['kind'] == 'native_tool' and e['data'].get('phase') == 'end']
    tests = [e for e in events if e['kind'] == 'verification']
    snapshots = [m for e in contexts for m in e['data'].get('memory_context', []) if m.get('id') == memory_id]
    return {'task_id': task_id, 'state': task['state'],
            'injected': any(memory_id in e['data'].get('memory_ids', []) for e in contexts),
            'memory_snapshots': snapshots, 'tool_counts': dict(Counter(e['data'].get('toolName') for e in tools)),
            'tool_calls': [{'evidence_id': e['seq'], 'tool': e['data'].get('toolName'),
                            'args': e['data'].get('args')} for e in tools],
            'tool_error_records': sum(bool(e['data'].get('isError')) for e in results),
            'verification': {k: tests[-1]['data'].get(k) for k in ('status', 'test_count', 'protected_intact')} if tests else {'status': 'unverified'},
            'evidence_ids': [e['seq'] for e in contexts + tests]}


def review(store, owner, project, memory_id=None):
    scopes = [(project, 'current_project')]
    rows = store.events(owner, project, 'cross_task_lab_state')
    lab = rows[-1]['data'] if rows else None
    if lab and lab.get('memory_id'):
        # Only the lab explicitly linked from this owner's current project is visible.
        scopes.append((lab['primary_project'], 'latest_linked_lab'))
    items = []
    for scope, source in scopes:
        for memory in Memory(store).list(owner, scope):
            if memory_id and memory['id'] != memory_id:
                continue
            if any(i['memory']['id'] == memory['id'] for i in items):
                continue
            contexts = store.events(owner, scope, 'context_assembled')
            ids = list(dict.fromkeys(e['task_id'] for e in contexts if e['task_id'] and
                       memory['id'] in e['data'].get('memory_ids', [])))
            usage = [task_summary(store, owner, scope, t, memory['id']) for t in ids]
            item = {'memory': {k: memory[k] for k in ('id','content','revision','scope','project')},
                    'source': source, 'usage': usage, 'assessment': 'insufficient_evidence',
                    'recommendation': '保留待评审；当前证据不足以判断收益或决定停用。'}
            if lab and source == 'latest_linked_lab' and memory['id'] == lab['memory_id']:
                arms = {}
                for name in ('baseline', 'memory'):
                    stage = lab['stages'][name]
                    if stage.get('task_id'):
                        arms[name] = task_summary(store, owner, scope, stage['task_id'], memory['id'])
                item['pair'] = arms
                a, b = lab['stages']['baseline'], lab['stages']['memory']
                valid = (all(s.get('status') == 'completed' and s.get('verification',{}).get('status') == 'passed' for s in (a,b))
                         and bool(a.get('model')) and a.get('model') == b.get('model')
                         and bool(a.get('initial_fingerprint')) and a.get('initial_fingerprint') == b.get('initial_fingerprint')
                         and 'baseline' in arms and 'memory' in arms and not arms['baseline']['injected'] and arms['memory']['injected']
                         and any(m.get('revision') == memory['revision'] and m.get('content') == memory['content']
                                 for m in arms['memory']['memory_snapshots']))
                if valid:
                    item['assessment'] = 'no_demonstrated_gain' if a.get('zero_test_calls') == b.get('zero_test_calls') else 'needs_further_review'
                    item['recommendation'] = '暂时保留，收益待验证；两组均通过，不能把工具调用更少直接算作提升。'
            items.append(item)
    if memory_id and not items:
        raise ValueError('Memory not found in the current scope or its latest linked lab')
    return {'memory_review': {'project': project, 'items': items,
            'method': 'local-evidence-summary-v1',
            'limitations': '仅汇总注入与执行证据；不证明因果收益或递归进化，不自动修改记忆。'}}
