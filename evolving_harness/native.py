"""Native Pi launcher and local one-request JSON bridge (no network listener)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys

from .bootstrap import bootstrap
from .domain import digest
from .evolution import Evolution, PiProposer, feedback
from .evidence import Evidence
from .memory import Memory
from .node import resolve_node
from .runtime import redact
from .reflection import Reflection
from .store import Store


def native_tools(allow_write: bool, platform: str | None = None) -> str:
    platform = platform or sys.platform
    tools = ['read', 'ls']
    if allow_write:
        tools.extend(['powershell' if platform == 'win32' else 'bash', 'edit', 'write'])
    # Offline Pi cannot fetch missing search binaries. Do not advertise broken tools.
    if shutil.which('rg'):
        tools.append('grep')
    if shutil.which('fd'):
        tools.append('find')
    return ','.join(tools)


def with_report(store: Store, owner: str, project: str, name: str, data: dict) -> dict:
    database = Path(store.db.execute('PRAGMA database_list').fetchone()[2])
    folder = database.parent / 'native-reports' / digest({'user': owner, 'project': project})[:16]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{name}-{digest(data)[:16]}.json'
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data | {'report_path': str(path)}


def dispatch(store: Store, owner: str, project: str, request: dict) -> dict:
    bootstrap(store, owner, project)
    memory = Memory(store)
    operation = request['operation']
    evidence = Evidence(store, owner, project)
    if operation == 'home':
        from .home import export
        return export(store,owner,project,request.get('workspace'))
    if (operation.startswith(('product-','component-','strategy-','tool-','experiments-'))
        or operation == 'skill-from-experience'):
        from .product import handle
        return with_report(store, owner, project, 'harness-center', handle(store,owner,project,request))
    if operation.startswith('memory-revision-'):
        from .memory_revision import Revisions
        revisions = Revisions(store, owner, project)
        action = operation.removeprefix('memory-revision-')
        if action in {'preview','model'}:
            from .revision_model import prepare, generate
            prepared = prepare(store, owner, project, request['memory_id'])
            if action == 'preview':
                result = prepared
            else:
                if request.get('confirmed') is not True or not request.get('provider') or not request.get('model'):
                    raise ValueError('Confirm outbound evidence and model before generation')
                if request.get('packet_hash') != prepared['packet_hash']:
                    raise ValueError('Preview changed; review the new payload first')
                result = {'revision_candidate':asyncio.run(generate(store,owner,project,prepared,
                    request['pi_root'],request['provider'],request['model']))}
        elif action == 'list':
            result = {'revision_candidates': revisions.list()}
        elif action == 'propose':
            result = {'revision_candidate': revisions.propose(request['memory_id'])}
        elif action == 'get':
            result = {'revision_candidate': revisions.get(request['id'])}
        elif action == 'edit':
            result = {'revision_candidate': revisions.edit(request['id'], request['revision'],
                request['action'], request.get('content'), request.get('reason'))}
        elif action == 'decide':
            if request.get('accept') is True and request.get('confirmed') is not True:
                raise ValueError('Confirm the exact revision candidate before accepting')
            result = {'revision_candidate': revisions.decide(request['id'], request['revision'], request['accept'])}
        elif action == 'rollback':
            if request.get('confirmed') is not True: raise ValueError('Confirm rollback before changing memory')
            result = {'revision_candidate':revisions.rollback(request['id'])}
        else:
            raise ValueError('Unknown memory revision operation')
        return with_report(store, owner, project, 'memory-revision', result)
    if operation == 'memory-review':
        from .memory_review import review
        return with_report(store, owner, project, 'memory-review', review(store, owner, project, request.get('memory_id')))
    if operation.startswith('lab-'):
        from .cross_task import CrossTaskLab
        lab=CrossTaskLab(store,owner,project)
        if operation=='lab-setup': result=lab.setup()
        elif operation=='lab-status': result=lab.status()
        elif operation=='lab-review': result=lab.review()
        elif operation=='lab-candidate': result={'lab':lab.current(),'candidate':lab.candidate()}
        elif operation=='lab-edit': result=lab.edit(request['content'],request['revision'])
        elif operation=='lab-accept':
            if request.get('confirmed') is not True: raise ValueError('Confirm the lab project memory first')
            result=lab.accept(request['id'],request['revision'])
        elif operation in {'lab-preview','lab-run'}:
            result=lab.preview(request['stage'])
            if operation=='lab-run':
                if os.environ.get('EH_ALLOW_WRITE')!='1': raise ValueError('Restart native with --allow-write for lab repair runs')
                if request.get('confirmed') is not True or not request.get('provider') or not request.get('model'):
                    raise ValueError('Confirm model payload and real shell execution first')
                if request.get('packet_hash')!=result['packet_hash']: raise ValueError('Lab state changed after preview; preview again')
                result=asyncio.run(lab.run(result,request['pi_root'],request['provider'],request['model']))
        else: raise ValueError('Unknown cross-task lab operation')
        return with_report(store,owner,project,'cross-task-lab',result)
    if operation in {'experiment-preview','experiment-run'}:
        from .experiment import prepare, run
        preview = prepare(store,owner,project,request.get('memory_id'))
        if operation == 'experiment-preview':
            return with_report(store,owner,project,'experiment-preview',preview)
        if request.get('confirmed') is not True or not request.get('provider') or not request.get('model'):
            raise ValueError('Confirm the paired experiment payload and select a model first')
        if request.get('packet_hash') != preview['packet_hash']:
            raise ValueError('Memory, fixture or instructions changed after preview; preview again')
        report=asyncio.run(run(store,owner,project,preview,request['pi_root'],request['provider'],request['model']))
        return with_report(store,owner,project,'experiment',report)
    if operation == 'experiment-latest':
        experiments=store.events(owner,project,'memory_experiment')
        if not experiments:
            raise ValueError('No experiment has run in this scope')
        return with_report(store,owner,project,'experiment',experiments[-1]['data'])
    if operation.startswith('review-') or operation.startswith('experience-'):
        reflection = Reflection(store, owner, project)
        if operation == 'review-preview':
            return with_report(store, owner, project, 'review-preview', reflection.prepare(request.get('task_id')))
        if operation == 'review-local':
            return with_report(store, owner, project, 'experiences',
                {'candidates':reflection.local(request.get('task_id')), 'backend':'local-feedback-extraction'})
        if operation == 'review-model':
            if request.get('confirmed') is not True or not request.get('provider') or not request.get('model'):
                raise ValueError('Confirm the outbound packet and select a model before reflection')
            prepared = reflection.prepare(request.get('task_id'))
            if request.get('packet_hash') != prepared['packet_hash']:
                raise ValueError('Evidence changed after preview; review the new preview first')
            candidates = asyncio.run(reflection.model(prepared, request['pi_root'],request['provider'],request['model']))
            return with_report(store,owner,project,'experiences',{'candidates':candidates,'backend':'pi-rpc-reflection'})
        if operation == 'experience-list':
            return with_report(store,owner,project,'experiences',{'candidates':reflection.list()})
        if operation == 'experience-get':
            return {'candidate':reflection.get(request['id'])}
        if operation == 'experience-edit':
            return {'candidate':reflection.edit(request['id'],request['content'],request['revision'])}
        if operation == 'experience-decide':
            if type(request.get('accept')) is not bool:
                raise ValueError('Decision must explicitly accept or reject')
            return {'candidate':reflection.decide(request['id'],request['accept'],request['revision'])}
        raise ValueError('Unknown reflection operation')
    if operation == 'task':
        return with_report(store, owner, project, 'task', evidence.bundle(request.get('task_id')))
    if operation == 'task-card':
        from .completion import export
        return export(store, owner, project, request.get('task_id'))
    if operation in {'task-rating-submit','task-rating-list','task-suggestion-decide'}:
        from .task_feedback import TaskFeedback
        from .completion import export
        task_ratings = TaskFeedback(store, owner, project)
        if operation == 'task-suggestion-decide':
            if request.get('confirmed') is not True: raise ValueError('Confirm this review; it will not apply a rule')
            result = task_ratings.decide(request['id'],request['revision'],request['action'])
            task = result['suggestions'][0]['task_id'] if result['suggestions'] else None
        else:
            task = request.get('task_id') or evidence.bundle()['task']['id']
            if operation == 'task-rating-submit':
                if request.get('confirmed') is not True: raise ValueError('Confirm feedback for the selected task')
                result = task_ratings.submit(task,request['rating'],request.get('target','task'),request.get('note',''),request['request_id'])
            else: result = task_ratings.list(task)
        return {'task_feedback':result, **export(store,owner,project,task)}
    if operation == 'usage':
        from .completion import number
        task = request['task_id']
        if store.get_task(owner, project, task)['state'] != 'running':
            raise ValueError('Task already settled')
        raw = request.get('usage')
        if not isinstance(raw, dict): raise ValueError('Usage must be an object')
        data = {key: number(raw.get(key)) for key in ('input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens')}
        cost = raw.get('cost')
        data['reported_cost'] = number(cost.get('total')) if isinstance(cost, dict) else None
        data['source'] = 'native-assistant-message-usage'
        return {'evidence_id': evidence.record(task, 'model_usage', data)}
    if operation == 'verify':
        if os.environ.get('EH_ALLOW_WRITE') != '1':
            raise ValueError('Running project tests requires restarting native with --allow-write')
        task = request.get('task_id')
        standalone = not task and bool(request.get('workspace'))
        if standalone:
            task = store.task(owner, project, 'Explicit local verification only; not an Agent repair task.')
            store.event(owner, project, 'context_assembled', {'workspace': request['workspace'],
                        'interface': 'native-user-verification'}, task)
            evidence.capture(task, 'begin')
        task = task or evidence.bundle()['task']['id']
        try:
            result = evidence.verify(task)
        except Exception as exc:
            if standalone:
                store.finish_task(task, 'failed', redact(str(exc)))
            raise
        if standalone:
            evidence.capture(task, 'finish')
            store.finish_task(task, 'completed', 'Local unittest outcome: ' + result['status'])
        from .completion import export
        return with_report(store, owner, project, 'task', evidence.bundle(task)) | export(store, owner, project, task)
    if operation == 'note':
        task = request.get('task_id') or evidence.bundle()['task']['id']
        note = request.get('note', '').strip()
        if not note:
            raise ValueError('Feedback note cannot be empty')
        return {'evidence_id': evidence.record(task, 'user_feedback', {'note': redact(note),
                               'source': 'explicit-native-user'})}
    if operation == 'status':
        return with_report(store, owner, project, 'status', {'user': owner, 'project': project, 'memories': len(memory.list(owner, project)),
                'versions': [{k: a[k] for k in ('name', 'id', 'version', 'parent', 'generated_by')}
                             for a in store.history(owner, project)],
                'active': {name: store.current(owner, project, name)['id']
                           for name in ('coding', 'memory-policy', 'improve')}})
    if operation == 'begin':
        task = store.task(owner, project, redact(request['prompt']))
        selected = memory.retrieve(owner, project, request['prompt'], task)
        from .personalization import assemble
        managed = assemble(store,owner,project,request['prompt'],selected)
        selected = managed['memories']
        coding = store.current(owner, project, 'coding')
        policy = store.current(owner, project, 'memory-policy')
        store.event(owner, project, 'context_assembled', {'skill_id': coding['id'],
                    'memory_ids': [m['id'] for m in selected], 'interface': 'native-pi',
                    'memory_context': redact([{k: m[k] for k in ('id', 'content', 'source', 'revision')}
                                              for m in selected]),
                    'session_id': request.get('session_id'), 'workspace': request.get('workspace'),
                    'model': request.get('model'), 'policy_id': policy['id'],
                    'components':managed['components'],'context_metrics':managed['context_metrics']}, task)
        evidence.capture(task, 'begin')
        from .improvement_batch import track
        track(store,owner,project,managed['components']['reviewer'],'task_context',task)
        return {'task_id': task, 'coding': coding['payload']['instructions']+'\n'+managed['instructions'],
                'skill_version': coding['version'], 'policy_version': policy['version'],
                'memories': [{k: m[k] for k in ('id', 'content', 'source')} for m in selected]}
    if operation in {'event', 'finish'}:
        task = request['task_id']
        stored = store.get_task(owner, project, task)
        if stored['state'] != 'running':
            raise ValueError('Task already settled')
        if operation == 'event':
            event = request['event']
            # Retain text-only observations; never hidden reasoning or binary content.
            observation = {key: event[key] for key in ('phase', 'toolCallId', 'parentToolCallId',
                'toolName', 'isError', 'args', 'duration_ms', 'output', 'exit_code', 'truncated') if key in event}
            store.event(owner, project, 'native_tool', redact(observation), task)
            return {'recorded': True}
        state = request['state']
        if state not in {'completed', 'failed', 'aborted', 'interrupted'}:
            raise ValueError('Invalid task outcome')
        from .completion import number
        elapsed = number(request.get('elapsed_ms'))
        if request.get('elapsed_ms') is not None and (elapsed is None or elapsed > 604800000):
            raise ValueError('Invalid task elapsed time')
        if elapsed is not None:
            evidence.record(task, 'execution_metrics', {'elapsed_seconds': elapsed / 1000,
                            'source': 'native-extension-clock'})
        evidence.capture(task, 'finish')
        store.finish_task(task, state, redact(request.get('text', '')))
        audit = memory.audit(owner, project)
        from .strategy import Strategy
        strategy_observation = Strategy(store,owner,project).observe_task(task)
        store.event(owner, project, 'native_settled', {'state': state, 'audit': audit}, task)
        from .completion import export
        return {'task_id': task, 'state': state, 'audit': audit,'strategy_observation':strategy_observation,
                **export(store, owner, project, task)}
    if operation == 'memory-list':
        return with_report(store, owner, project, 'memories', {'memories': memory.list(owner, project)})
    if operation == 'memory-add':
        return {'id': memory.add(owner, project, request['content'], 'explicit-native-user',
                                 scope=request.get('scope', 'project'), verified=True)}
    if operation == 'memory-edit':
        memory.edit(owner, project, request['id'], request['content'], 'explicit-native-user', True)
        return {'updated': request['id']}
    if operation == 'memory-forget':
        memory.forget(owner, project, request['id'])
        return {'forgotten': request['id']}
    if operation == 'feedback':
        return {'event': feedback(store, owner, project, request['defect'], request['note'],
                                   task_id=request.get('task_id'))}
    if operation == 'audit':
        return memory.audit(owner, project)
    if operation == 'rollback':
        if request['name'] not in {'memory-policy', 'improve'}:
            raise ValueError('Unsupported rollback target')
        return store.rollback(owner, project, request['name'])
    if operation == 'evolution-preview':
        evidence = [e for e in store.events(owner, project)
                    if e['kind'] in {'feedback', 'improvement_attempt'}][-100:]
        return with_report(store, owner, project, 'preview', {'evidence': evidence,
                'active_policy': store.current(owner, project, 'memory-policy')['payload'],
                'active_improver': store.current(owner, project, 'improve')['payload'],
                'notice': 'Model receives active policy/improver and this scoped evidence; '
                'no tool access. Subsequent rounds also receive new attempt summaries.'})
    if operation == 'evolve':
        if request.get('confirmed') is not True:
            raise ValueError('Explicit outbound-data confirmation required')
        async def evolve():
            async with asyncio.timeout(300):
                return await Evolution(store, owner, project, PiProposer(
                    request['pi_root'], 90, request.get('provider'), request.get('model'))).cycle(
                        rounds=2, apply=bool(request.get('apply')))
        return with_report(store, owner, project, 'evolution', asyncio.run(evolve()))
    raise ValueError('Unknown native bridge operation')


def bridge_main() -> int:
    try:
        request = json.load(sys.stdin)
        store = Store(os.environ['EH_DB'])
        try:
            result = dispatch(store, os.environ['EH_USER'], os.environ['EH_PROJECT'], request)
        finally:
            store.close()
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': redact(str(exc))}, ensure_ascii=False))
        return 1


def launch(args) -> int:
    root = Path(__file__).resolve().parents[1]
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        raise ValueError('Workspace must exist')
    node = resolve_node()
    entry = Path(args.pi_root).resolve() / 'packages/coding-agent/dist/bundle/cli.js'
    if not entry.is_file():
        raise ValueError(f'Pi build not found: {entry}')
    db = Path(args.db).resolve()
    project = args.project if args.project != 'default' else 'workspace-' + digest(str(workspace))[:16]
    env = os.environ.copy()
    env.update(EH_HOME=str(root), EH_PYTHON=sys.executable, EH_DB=str(db), EH_USER=args.user,
               EH_PROJECT=project, EH_PI_ROOT=str(Path(args.pi_root).resolve()), EH_NODE=node,
               PYTHONUTF8='1')
    env['EH_ALLOW_WRITE'] = '1' if args.allow_write else '0'
    env['PYTHONPATH'] = str(root) + os.pathsep + env.get('PYTHONPATH', '')
    env['PATH'] = str(Path(node).parent) + os.pathsep + env.get('PATH', '')
    command = [node, str(entry), '--offline', '--extension', str(root / 'extensions/evolving.ts')]
    if not getattr(args, 'with_user_extensions', False):
        command.append('--no-extensions')
    command.extend(['--tools', native_tools(args.allow_write)])
    command.extend(['--thinking', getattr(args, 'thinking', 'medium')])
    scope = digest({'user': args.user, 'project': project, 'workspace': str(workspace)})[:24]
    sessions = db.parent / 'native-sessions' / scope
    sessions.mkdir(parents=True, exist_ok=True)
    command.extend(['--session-dir', str(sessions)])
    if args.resume:
        command.append('--continue')
    for flag in ('provider', 'model'):
        if getattr(args, flag):
            command.extend(['--' + flag, getattr(args, flag)])
    print(f'原生 Pi + Evolving Harness | {project} | {workspace}', flush=True)
    return subprocess.call(command, cwd=workspace, env=env)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(bridge_main())
