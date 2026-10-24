"""One paired real-code task with a single Skill intervention and independent checks."""
import ast
import asyncio
import hashlib
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

from .components import Components
from .domain import digest,uid
from .evidence import Evidence,snapshot
from .memory import Memory,select_memories
from .personalization import budget_memories
from .rpc import PiRpc,pi_command
from .runtime import redact

TASK='''将日志工具的文本和JSONL输入解析函数从 log_inspector/core.py 移到新的
log_inspector/parsers.py，保留 core 中原公开导入接口的兼容性，不改变CLI现有行为。
更新相关导入和README，添加必要的回归测试，运行项目测试并报告结果。
只能修改当前副本；不要删除或修改已有测试、AGENTS.md、checks/run_tests.py，允许增加新测试。
不要安装依赖、联网或读取其他目录。'''
EXCLUDE={'.harness','.git','__pycache__','.venv','venv','node_modules'}
SUFFIXES={'.py','.md','.toml','.json','.jsonl','.log','.txt'}


def linked(path):
    return path.is_symlink() or getattr(path,'is_junction',lambda:False)()


def source_files(root):
    root=Path(root).resolve();files={};total=0
    for base,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if d not in EXCLUDE and not d.startswith('.')]
        for d in dirs:
            if linked(Path(base)/d):raise ValueError('Source junction/symlink is not supported')
        for name in names:
            p=Path(base)/name
            if p.suffix.lower() not in SUFFIXES:continue
            if linked(p):raise ValueError('Source symlink is not supported')
            if len(files)>=80 or p.stat().st_size>128000:raise ValueError('Comparison source exceeds bounded file scope')
            text=p.read_text(encoding='utf-8');total+=len(text.encode('utf-8'))
            if total>1000000:raise ValueError('Comparison source exceeds 1MB')
            files[p.relative_to(root).as_posix()]=text
    for required in ('AGENTS.md','checks/run_tests.py','log_inspector/core.py','log_inspector/__main__.py','tests/test_logs.py'):
        if required not in files:raise ValueError('Run comparison in the supported log-inspector project')
    if 'log_inspector/parsers.py' in files:raise ValueError('Extraction task is already implemented; do not replay it as a new task')
    return files


def protected_ok(root,files):
    try:
        for name,text in files.items():
            if name=='AGENTS.md' or name=='checks/run_tests.py' or name.startswith('tests/'):
                p=root/name
                if linked(p) or not p.resolve().is_relative_to(root.resolve()) or p.read_text(encoding='utf-8')!=text:return False
        return True
    except (OSError,UnicodeError):return False


def reference(root,files):
    root=Path(root)
    if not protected_ok(root,files):return {'status':'protected_changed'}
    before=snapshot(str(root))
    env=os.environ.copy();env.pop('PYTHONPATH',None);env['PYTHONUTF8']='1'
    outputs=[]
    probe="from log_inspector import core,parsers; from pathlib import Path; import ast; tree=ast.parse(Path('log_inspector/parsers.py').read_text(encoding='utf-8')); assert {'parse_line','parse_json_line'} <= {x.name for x in tree.body if isinstance(x,ast.FunctionDef)}; text='2026-10-24T01:00:00Z INFO ok'; assert core.parse_line(text,7).as_dict()==parsers.parse_line(text,7).as_dict(); raw='{\"timestamp\":\"2026-10-24T01:00:00Z\",\"level\":\"WARN\",\"message\":\"ok\"}'; assert core.parse_json_line(raw,2).as_dict()==parsers.parse_json_line(raw,2).as_dict(); print('EXTRACTION_OK')"
    for args in ([sys.executable,'-I','-X','utf8','checks/run_tests.py'],[sys.executable,'-X','utf8','-c',probe]):
        try:
            with tempfile.TemporaryFile() as capture:
                p=subprocess.Popen(args,cwd=root,env=env,stdout=capture,stderr=subprocess.STDOUT)
                try:p.wait(timeout=15)
                except subprocess.TimeoutExpired:p.kill();p.wait();return {'status':'timeout'}
                capture.seek(0);text=capture.read(24000).decode('utf-8',errors='replace')
            outputs.append({'command':args,'exit_code':p.returncode,'output':redact(text)})
        except OSError as exc:return {'status':'failed','error':redact(str(exc))}
    match=re.search(r'Ran (\d+) tests?\b',outputs[0]['output']);count=int(match[1]) if match else 0
    intact=protected_ok(root,files);stable=before.get('manifest_hash')==snapshot(str(root)).get('manifest_hash')
    status='passed' if intact and stable and count>0 and all(o['exit_code']==0 for o in outputs) else 'failed'
    return {'status':status,'test_count':count,'protected_intact':intact,'snapshot_stable':stable,'checks':outputs}


def compare(results):
    if set(results)!={'without_skill','with_skill'} or any(r['status']!='completed' for r in results.values()):
        return {'outcome':'inconclusive'}
    a,b=results['without_skill'],results['with_skill']
    if any(not r['reference'].get('protected_intact',True) or r['reference']['status'] in {'timeout','protected_changed'} for r in (a,b)):
        return {'outcome':'inconclusive','reason':'Invalid independent evaluation'}
    x,y=(r['reference']['status']=='passed' for r in (a,b))
    outcome='improved_on_this_task' if y and not x else 'worse_on_this_task' if x and not y else 'tie_on_this_task' if x and y else 'both_failed'
    return {'outcome':outcome,'without_passed':x,'with_passed':y,
        'isolated_check_observed':{k:v['metrics']['isolated_check_observed'] for k,v in results.items()},
        'notice':'Extra checks and fewer calls are not automatically performance gains. One pair is not statistical evidence.'}


class SkillCompare:
    event_kind = 'skill_comparison_state'
    preview_key = 'skill_comparison_preview'
    result_key = 'skill_comparison_result'
    interface = 'skill-comparison'

    def collect(self, root): return source_files(root)
    def verify(self, root, state): return reference(root, state['files'])
    def workflow(self, name, state): return state['skill'] if name == 'with_skill' else None
    def outcome(self, results): return compare(results)

    def __init__(self,store,owner,project):self.store,self.owner,self.project=store,owner,project

    def current(self):
        events=self.store.events(self.owner,self.project,self.event_kind)
        if not events:raise ValueError('Prepare the comparison first')
        return events[-1]['data']

    def save(self,state):self.store.event(self.owner,self.project,self.event_kind,state)

    def setup(self,workspace):
        files=source_files(workspace)
        registry=Components(self.store,self.owner,self.project);skill=registry.active('skill:portable-test-entry')
        if not skill:raise ValueError('Activate portable-test-entry before comparing it')
        profile=Components(self.store,self.owner,'@user').seed('profile')
        context=registry.seed('context');policy=self.store.current(self.owner,self.project,'memory-policy')
        memories=select_memories(Memory(self.store).list(self.owner,self.project),policy['payload'],owner=self.owner,project=self.project,query=TASK)
        memories=budget_memories(memories,context['payload'])
        coding=self.store.current(self.owner,self.project,'coding')
        parent=Path(self.store.db.execute('PRAGMA database_list').fetchone()[2]).parent/'skill-comparisons'/uid('pair')
        arms={}
        for name in ('without_skill','with_skill'):
            root=parent/('workspace-'+secrets.token_hex(4));root.mkdir(parents=True)
            for path,text in files.items():
                target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text,encoding='utf-8')
            arms[name]={'workspace':str(root),'status':'ready'}
        state={'id':parent.name,'status':'ready','source':str(Path(workspace).resolve()),'files':files,'source_hash':digest(files),
            'skill':skill,'coding':coding['payload']['instructions'],'profile':profile['payload'],
            'memories':[{k:m[k] for k in ('id','revision','content')} for m in memories],
            'task':TASK,'arms':arms,'order':secrets.SystemRandom().sample(list(arms),2),'results':{},
            'limits':'Real shell with user permissions, not an OS sandbox. Source copies isolate normal writes only. One pair, no automatic promotion.'}
        self.save(state);return self.preview()

    def preview(self):
        state=self.current()
        return {self.preview_key:state,'packet_hash':digest(state)}

    async def run(self,prepared,pi_root,provider,model):
        from .native import native_tools
        state=self.current()
        if state['status']!='ready' or digest(state)!=prepared['packet_hash']:raise ValueError('Comparison already attempted or preview stale')
        for arm in state['arms'].values():
            if self.collect(arm['workspace'])!=state['files']:raise ValueError('Workspace copy changed after preview')
        # Claim before network activity; never silently replay after failure.
        with self.store.transaction():
            if self.current()['status']!='ready':raise ValueError('Comparison already claimed')
            state['status']='running'
            from .domain import canonical,now
            self.store.db.execute('INSERT INTO events(owner,project,kind,data,created) VALUES(?,?,?,?,?)',
                (self.owner,self.project,self.event_kind,canonical(state),now()))
        common=state['coding']+'\nUser preferences:\n'+str(state['profile'])+'\nScoped memories:\n'+str(state['memories'])+'\n'+state['task']
        for name in state['order']:
            arm=state['arms'][name];root=Path(arm['workspace']);observations=[];usages=[];started=time.monotonic()
            totals={'tool_calls':0,'tool_errors':0,'tool_events':0}
            task=self.store.task(self.owner,self.project,'Paired evaluation: '+state['task'])
            evidence=Evidence(self.store,self.owner,self.project)
            evidence.record(task,'context_assembled',{'workspace':str(root),'interface':self.interface,'arm':name,
                'comparison_id':state['id'],'skill_ids':[self.workflow(name,state)['id']] if self.workflow(name,state) else [],
                'memory_context':state['memories'],'source_hash':state['source_hash']})
            evidence.capture(task,'begin')
            workflow=self.workflow(name,state)
            prompt=common+ ('\nManaged workflow:\n'+'\n'.join(workflow['payload']['steps']) if workflow else '')
            command=pi_command(pi_root,tools=True,provider=provider,model=model)+['--tools',native_tools(True),'--thinking','off']
            def observe(event):
                if event.get('type')=='message_end':
                    message=event.get('message') or {}
                    if message.get('role')=='assistant' and isinstance(message.get('usage'),dict):usages.append(message['usage'])
                    return
                if event.get('type') not in ('tool_execution_start','tool_execution_end'):return
                totals['tool_events']+=1
                if event['type']=='tool_execution_start':totals['tool_calls']+=1
                elif event.get('isError',False):totals['tool_errors']+=1
                data={'phase':'start' if event['type'].endswith('start') else 'end','toolName':event.get('toolName'),
                    'toolCallId':event.get('toolCallId'),'args':redact(event.get('args')),
                    'output':redact('\n'.join(b.get('text','') for b in (event.get('result') or {}).get('content',[]) if b.get('type')=='text'))[:12000],
                    'isError':event.get('isError',False)}
                if len(observations)<100:observations.append(data);evidence.record(task,'native_tool',data)
            error=None;text='';usage=None
            try:
                if self.collect(root)!=state['files']:raise ValueError('Workspace changed before its assigned run')
                async with asyncio.timeout(115):
                    async with PiRpc(command,root,100,observe) as rpc:
                        await rpc.request('set_auto_retry',enabled=False)
                        selected=(await rpc.request('get_state')).get('model') or {}
                        if (selected.get('provider'),selected.get('id'))!=(provider,model):raise ValueError('Confirmed model mismatch')
                        result=await rpc.prompt(prompt);text=redact(result['text'])
                        usage=redact((result.get('message') or {}).get('usage'))
            except Exception as exc:error=redact(str(exc)) or type(exc).__name__
            checked=self.verify(root,state);evidence.record(task,'verification',checked);evidence.capture(task,'finish')
            self.store.finish_task(task,'failed' if error else 'completed',text or error)
            starts={o['toolCallId']:o for o in observations if o['phase']=='start'}
            isolated=any(o['phase']=='end' and not o['isError'] and re.search(r'Ran [1-9]\d* tests?',o['output'])
                and '-I' in str(starts.get(o['toolCallId'],{}).get('args','')) and o['toolName'] in {'powershell','bash'} for o in observations)
            state['results'][name]={'status':'failed' if error else 'completed','task_id':task,'error':error,'text':text,
                'reference':checked,'metrics':{'isolated_check_observed':isolated,
                    'tool_calls':totals['tool_calls'],'tool_errors':totals['tool_errors'],
                    'trace_truncated':totals['tool_events']>len(observations),'elapsed_seconds':round(time.monotonic()-started,2),
                    'observed_usage':{k:sum(u[k] for u in usages if isinstance(u.get(k),(int,float))) if any(isinstance(u.get(k),(int,float)) for u in usages) else None
                                      for k in ('input','output','cacheRead','cacheWrite','totalTokens')},
                    'reported_cost':sum(u['cost']['total'] for u in usages if isinstance(u.get('cost'),dict) and isinstance(u['cost'].get('total'),(int,float))) if any(isinstance(u.get('cost'),dict) and isinstance(u['cost'].get('total'),(int,float)) for u in usages) else None,
                    'last_message_usage':usage}}
            arm['status']=state['results'][name]['status']
            self.save(state)
        try:original_unchanged=self.collect(state['source'])==state['files']
        except (ValueError,OSError,UnicodeError):original_unchanged=False
        outcome=self.outcome(state['results']) if original_unchanged else {'outcome':'inconclusive','reason':'Original source changed during experiment'}
        state.update(status='completed',model={'provider':provider,'id':model},comparison=outcome,original_source_unchanged=original_unchanged)
        self.save(state);return {self.result_key:state}
