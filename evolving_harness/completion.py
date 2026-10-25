"""Evidence-derived task cards. Model prose is never verification evidence."""
import html
import json
import math
from pathlib import Path
import sqlite3

from .domain import digest
from .evidence import Evidence


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def build(store, owner, project, task=None):
    bundle = Evidence(store, owner, project).bundle(task)
    task = bundle['task']; events = bundle['events']; summary = bundle['summary']
    contexts = [e for e in events if e['kind'] == 'context_assembled']
    provenance = [e for e in events if e['kind'] == 'execution_provenance']
    metrics = [e for e in events if e['kind'] == 'execution_metrics']
    verifications = [e for e in events if e['kind'] == 'verification']
    snapshots = [e for e in events if e['kind'] == 'code_snapshot']
    usage = [e['data'] for e in events if e['kind'] == 'model_usage']
    context = contexts[-1]['data'] if contexts else {}
    origin = provenance[-1]['data'] if provenance else {}
    metric = metrics[-1]['data'] if metrics else {}
    verified = verifications[-1]['data'] if verifications else {}
    cost = None; missing_cost = 0; tokens = None
    if usage:
        costs = [number(u.get('reported_cost')) for u in usage]
        missing_cost = sum(c is None for c in costs)
        cost = sum(costs) if not missing_cost else None
        counts = [number(u.get('totalTokens')) for u in usage]
        tokens = sum(counts) if all(c is not None for c in counts) else None
    else:
        cost = number(metric.get('reported_cost')); tokens = number(metric.get('tokens'))
    known_cost = sum(number(u.get('reported_cost')) or 0 for u in usage) if usage else cost
    refs = []
    refs.extend(context.get('skill_ids') or [])
    refs.extend((context.get('components') or {}).get('skills') or [])
    if context.get('skill_id'): refs.append(context['skill_id'])
    if origin.get('active_skill_id'): refs.append(origin['active_skill_id'])
    skills = []
    for key in dict.fromkeys(refs):
        row = None
        for table in ('components', 'artifacts'):
            try:
                row = store.db.execute('SELECT id,name,version,hash FROM '+table+' WHERE id=? AND owner=? AND project=?',
                                       (key, owner, project)).fetchone()
            except sqlite3.OperationalError: pass
            if row: break
        skills.append(dict(row) if row else {'id': key, 'name': None, 'version': None, 'hash': None})
    counts = [c.get('test_count') for c in verified.get('checks', [])]
    test_count = number(verified.get('test_count'))
    if test_count is None:
        valid_counts = [number(c) for c in counts if number(c) is not None]
        test_count = max(valid_counts) if valid_counts else None
    protection = verified.get('protected_intact')
    if type(protection) is not bool: protection = None
    integrity = verified.get('integrity_ok')
    if type(integrity) is not bool: integrity = None
    status = verified.get('status', 'unverified')
    fresh = summary['verification_freshness']
    if task['state'] != 'completed':
        decision, action = 'needs_attention', '先处理执行失败或中断；保留记录，不自动重试。'
    elif protection is False or integrity is False:
        decision, action = 'needs_fix', '修复保护文件或工作区完整性问题，再重新验收。'
    elif status not in {'passed', 'unverified'}:
        decision, action = 'needs_fix', '查看独立验收失败证据，再修复代码。'
    elif status != 'passed' or not test_count or fresh != 'matches_recorded_text_scope':
        decision, action = 'needs_verification', '需要对当前代码执行独立验收；模型声称通过不算验证。'
    elif protection is None:
        decision, action = 'needs_protection_check', '测试已通过；保护文件是否违规尚未验证。'
    else:
        decision, action = 'completed', '本任务完成；保留证据，单次成功不证明进化收益。'
    snapshot_known = len(snapshots) >= 2 and all(e['data'].get('snapshot', {}).get('available') for e in (snapshots[0], snapshots[-1]))
    complete_scope = snapshot_known and all(e['data']['snapshot'].get('coverage') == 'within_declared_text_scope' for e in (snapshots[0], snapshots[-1]))
    relevant = {'context_assembled', 'execution_provenance', 'execution_metrics', 'model_usage',
                'verification', 'code_snapshot', 'user_feedback', 'task_suggestion_review'}
    from .task_feedback import TaskFeedback
    feedback = TaskFeedback(store,owner,project).list(task['id'])
    latest_rating = feedback['ratings'][-1]['rating'] if feedback['ratings'] else 'unrated'
    if latest_rating in {'adjust','issue'}:
        action = '用户反馈待跟进；查看建议及依据，不自动修改规则。' + action
    card = {'task_id': task['id'], 'user': owner, 'project': project,
        'request': task['prompt'][:1200], 'execution_state': task['state'],
        'decision': decision, 'next_action': action,
        'user_outcome': latest_rating, 'task_feedback': feedback,
        'changed_files': summary['changed_files'] if snapshot_known else None,
        'change_scope': 'declared_text_scope' if complete_scope else 'partial' if snapshot_known else 'unverified',
        'verification': {'status': status, 'test_count': test_count, 'freshness': fresh,
            'protected_intact': protection, 'integrity_ok': integrity, 'violations': verified.get('violations', [])},
        'model': origin.get('model') or context.get('model'), 'skills': skills,
        'execution_source': origin.get('source') or context.get('interface'),
        'elapsed_seconds': number(metric.get('elapsed_seconds')),
        'reported_cost': cost, 'known_reported_cost': known_cost, 'missing_cost_messages': missing_cost,
        'cost_kind': 'reported_estimate_not_bill', 'tokens': tokens,
        'tools': {'recorded_completed': summary['tool_records'], 'recorded_errors': summary['tool_errors']},
        'review_signal': bool(summary['tool_errors'] or any(e['kind'] == 'user_feedback' and e['data'].get('rating') != 'satisfied' for e in events) or decision == 'needs_fix'),
        'evidence_ids': [e['seq'] for e in events if e['kind'] in relevant],
        'automatic_retry': False, 'automatic_upgrade': False,
        'notice': '未验证不等于通过。记录只覆盖声明的文本范围；不推断缺失版本、费用或保护结果，不将单次成功作为进化收益。'}
    return card, bundle


def export(store, owner, project, task=None):
    card, bundle = build(store, owner, project, task)
    database = Path(store.db.execute('PRAGMA database_list').fetchone()[2])
    folder = database.parent / 'task-cards' / digest({'user': owner, 'project': project})[:16] / digest(card['task_id'])[:16]
    folder.mkdir(parents=True, exist_ok=True)
    evidence_path = folder / ('evidence-' + digest(bundle)[:16] + '.json')
    evidence_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding='utf-8')
    key = digest(card)[:16]; path = folder / ('card-' + key + '.json'); page = folder / ('card-' + key + '.html')
    path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding='utf-8')
    labels = {'completed': '任务完成', 'needs_attention': '执行需要处理', 'needs_fix': '需要修复',
              'needs_verification': '需要独立验证', 'needs_protection_check': '需要核对保护文件'}
    def text(value): return html.escape('未验证' if value is None else str(value))
    v = card['verification']; model = card['model'] or {}
    body = '<!doctype html><html lang="zh"><meta charset="utf-8"><title>任务完成卡片</title>'
    body += '<style>body{font:17px system-ui;max-width:1000px;margin:36px auto;padding:16px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:10px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#1263b8}</style>'
    body += '<h1>任务完成卡片 · ' + text(labels[card['decision']]) + '</h1><p>' + text(card['request']) + '</p>'
    fields = [('执行状态', card['execution_state']), ('独立验收', v['status']), ('实际测试数', v['test_count']),
        ('代码与验收记录匹配', v['freshness']), ('保护文件', '未验证' if v['protected_intact'] is None else '未发现违规' if v['protected_intact'] else '存在违规'),
        ('修改文件', ', '.join(card['changed_files']) if card['changed_files'] else '未记录到差异（限声明范围）' if card['changed_files'] == [] else None),
        ('模型', (str(model.get('provider', '?')) + ' / ' + str(model.get('id', model.get('model', '?')))) if model else None),
        ('Skill / coding版本', ', '.join((s['name'] or s['id']) + (' v'+str(s['version']) if s['version'] is not None else '（版本未验证）') for s in card['skills']) or None),
        ('耗时（秒）', card['elapsed_seconds']), ('报告估算费用（非账单）', card['reported_cost']),
        ('记录的工具完成 / 错误', str(card['tools']['recorded_completed']) + ' / ' + str(card['tools']['recorded_errors'])),
        ('下一步', card['next_action'])]
    body += '<table>' + ''.join('<tr><th>'+text(k)+'</th><td>'+text(v)+'</td></tr>' for k,v in fields) + '</table>'
    body += '<p>'+text(card['notice'])+'</p><p><a href="'+evidence_path.name+'">查看完整执行、差异及验证证据</a> · <a href="'+path.name+'">卡片JSON</a></p>'
    ratings = {'unrated':'尚未反馈','satisfied':'满意','adjust':'需要调整','issue':'执行有问题'}
    body += '<h2>用户反馈 · '+text(ratings[card['user_outcome']])+'</h2>'
    for item in card['task_feedback']['ratings'][-5:]:
        body += '<p>'+text(ratings[item['rating']])+' · '+text(item['note'] or '未补充说明')+' · 证据 '+text(item['evidence_id'])+' · '+text(item['source'])+'</p>'
    for suggestion in card['task_feedback']['suggestions'][-5:]:
        status_label = {'no_change':'暂不改进','pending':'待审核','reviewed_not_applied':'已审阅，未启用','rejected':'已拒绝'}[suggestion['status']]
        body += '<section><h3>建议 '+text(suggestion['id'])+' · '+text(status_label)+'</h3><p>'+text(suggestion['reason'])+'</p><p>'+text(suggestion['proposal'])+'</p><p>依据：'+text(suggestion['evidence_ids'])+'；目标 '+text(suggestion['target'])+'；修订 '+text(suggestion['revision'])+'。已审阅不等于已启用。</p></section>'
    body += '<h2>提交本任务反馈</h2><p>点击只生成命令，不会自动保存。复制到 Pi 后确认，才会写入本地；不会调用模型。</p>'
    body += '<label>说明 <textarea id="feedback-note" rows="3" maxlength="2000" style="width:100%"></textarea></label><p><label>建议范围 <select id="feedback-target"><option value="task">仅调整本任务</option><option value="memory">项目记忆草案</option><option value="workflow">Skill工作流假设</option></select></label></p>'
    body += '<p><button type="button" data-rating="satisfied">满意</button> <button type="button" data-rating="adjust">需要调整</button> <button type="button" data-rating="issue">执行有问题</button></p><input id="feedback-command" readonly style="width:100%" aria-label="复制到Pi的反馈命令"><p id="feedback-message" role="status"></p>'
    task_literal = json.dumps(card['task_id']).replace('<','\\u003c')
    body += '<script>const taskId='+task_literal+''';
document.querySelectorAll('[data-rating]').forEach(button=>button.addEventListener('click',async()=>{
const rating=button.dataset.rating,note=document.getElementById('feedback-note').value.trim(),message=document.getElementById('feedback-message');
if(rating!=='satisfied'&&!note){message.textContent='请说明需要调整的地方或问题。';return;}
const request_id=globalThis.crypto?.randomUUID?.()||('feedback-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2));
const command='/harness rate '+JSON.stringify({task_id:taskId,rating,target:rating==='satisfied'?'task':document.getElementById('feedback-target').value,note,request_id});
const field=document.getElementById('feedback-command');field.value=command;field.focus();field.select();
try{await navigator.clipboard.writeText(command);message.textContent='命令已复制。请粘贴到Pi并确认；当前尚未保存反馈。';}catch{message.textContent='请手动复制上方命令到Pi并确认；当前尚未保存反馈。';}
}));</script>'''
    body += '<details><summary>证据编号与全部字段</summary><pre>'+html.escape(json.dumps(card, ensure_ascii=False, indent=2))+'</pre></details></html>'
    page.write_text(body, encoding='utf-8')
    return {'completion_card': card, 'card_html': str(page), 'card_json': str(path), 'evidence_path': str(evidence_path)}
