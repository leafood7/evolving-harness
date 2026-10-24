"""Staged real-code lab: discover a project test entry, confirm memory, then holdout tasks."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

from .bootstrap import bootstrap
from .domain import digest, uid
from .evidence import Evidence, snapshot
from .memory import Memory
from .reflection import Reflection
from .rpc import PiRpc, pi_command
from .runtime import redact
from .store import Store

RULES = 'Only change rules.py. Do not alter tests, project.toml, AGENTS.md or test runners. Inspect project files to find verification instructions. Do not install packages or access files outside this workspace.'
RUNNER = '''import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
suite=unittest.defaultTestLoader.discover(str(Path(__file__).parent),pattern='case_*.py')
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() and result.testsRun else 1)
'''
TRAIN_CODE = 'def discounted_total(price, percent):\n    return price * (1 - percent)\n'
HOLDOUT_CODE = 'def paginate(items, page, size):\n    return items[page * size:(page + 1) * size]\n'
OTHER_CODE = 'def slug(text):\n    return text.strip().replace(" ", "-")\n'
TRAIN_TEST = '''import unittest
from rules import discounted_total
class DiscountTests(unittest.TestCase):
 def test_discount(self): self.assertEqual(discounted_total(100,20),80)
 def test_no_discount(self): self.assertEqual(discounted_total(25,0),25)
'''
HOLDOUT_TEST = '''import unittest
from rules import paginate
class PageTests(unittest.TestCase):
 def test_first(self): self.assertEqual(paginate([1,2,3,4],1,2),[1,2])
 def test_second(self): self.assertEqual(paginate([1,2,3,4],2,2),[3,4])
'''
OTHER_TEST = '''import unittest
from rules import slug
class SlugTests(unittest.TestCase):
 def test_lower(self): self.assertEqual(slug('Hello World'),'hello-world')
 def test_strip(self): self.assertEqual(slug(' Foo '),'foo')
'''


def files_for(stage: str) -> dict:
    if stage=='other':
        return {'AGENTS.md':RULES,'rules.py':OTHER_CODE,'test_slug.py':OTHER_TEST,
                'project.toml':'[testing]\ncommand="python -m unittest -v"\n'}
    return {'AGENTS.md':RULES,'rules.py':TRAIN_CODE if stage=='train' else HOLDOUT_CODE,
            'checks/case_rules.py':TRAIN_TEST if stage=='train' else HOLDOUT_TEST,
            'checks/run_tests.py':RUNNER,'project.toml':'[testing]\ncommand="python checks/run_tests.py"\n'}


def reference(workspace: Path, stage: str, root_probe: bool = False) -> dict:
    expected=files_for(stage)
    protected=[name for name in expected if name!='rules.py']
    def protected_intact():
        try:
            return all((workspace/name).is_file() and not (workspace/name).is_symlink() and
                (workspace/name).resolve().is_relative_to(workspace.resolve()) and
                (workspace/name).read_text(encoding='utf-8')==expected[name] for name in protected)
        except (OSError,UnicodeError): return False
    intact=protected_intact()
    if not intact:
        return {'status':'protected_files_changed','test_count':None,'exit_code':None}
    before=snapshot(str(workspace))
    command=[sys.executable,'-m','unittest','-v'] if stage=='other' or root_probe else [sys.executable,'checks/run_tests.py']
    with tempfile.TemporaryDirectory(prefix='lab-bytecode-') as cache, tempfile.TemporaryFile() as output:
        env=os.environ.copy();env.update(PYTHONUTF8='1',PYTHONPYCACHEPREFIX=cache,PYTHONDONTWRITEBYTECODE='1')
        process=subprocess.Popen(command,cwd=workspace,env=env,stdout=output,stderr=subprocess.STDOUT,shell=False)
        timeout=False
        try: process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            timeout=True;process.kill();process.wait()
        output.seek(0);text=output.read(24000).decode('utf-8',errors='replace')
    after=snapshot(str(workspace))
    match=re.search(r'Ran (\d+) tests?\b',text)
    count=int(match.group(1)) if match else None
    stable=before.get('manifest_hash')==after.get('manifest_hash')
    # Recheck protected files after execution as well as before it.
    intact=protected_intact()
    status=('timeout' if timeout else 'protected_files_changed' if not intact else
            'code_changed_during_test' if not stable else 'no_tests' if count==0 else
            'passed' if process.returncode==0 and count==2 else 'failed')
    return {'source':'lab-controlled-reference','command':command,'cwd':str(workspace),'status':status,
            'exit_code':process.returncode,'test_count':count,'output':redact(text),
            'before_snapshot':before,'after_snapshot':after,'snapshot_stable':stable,'protected_intact':intact}


class CrossTaskLab:
    def __init__(self,store: Store,owner: str,project: str):
        self.store,self.owner,self.project=store,owner,project

    def save(self,state: dict):
        self.store.event(self.owner,self.project,'cross_task_lab_state',state)

    def current(self) -> dict:
        rows=self.store.events(self.owner,self.project,'cross_task_lab_state')
        if not rows: raise ValueError('Run /harness lab setup first')
        return rows[-1]['data']

    def setup(self) -> dict:
        database=Path(self.store.db.execute('PRAGMA database_list').fetchone()[2])
        lab_id=uid('lab')
        root=database.parent/'cross-task-labs'/lab_id
        root.mkdir(parents=True,exist_ok=False)
        state={'id':lab_id,'root':str(root),'primary_project':lab_id+':primary',
               'other_project':lab_id+':other','stages':{},'memory_id':None,'attempted_order':[],
               'pair_order':['baseline','memory'] if secrets.randbelow(2) else ['memory','baseline'],
               'coding':self.store.current(self.owner,self.project,'coding'),
               'limitations':['Small real-code lab; no statistical or recursive-improvement claim.',
                   'Real shell tools run with user permissions; workspace copies are not an OS sandbox.']}
        for project in (state['primary_project'],state['other_project']): bootstrap(self.store,self.owner,project)
        for stage in ('train','baseline','memory','other'):
            # Neutral directory names avoid disclosing the treatment label through Pi's cwd.
            workspace=root/('holdout-'+secrets.token_hex(4) if stage in {'baseline','memory'} else stage)
            for name,text in files_for(stage).items():
                path=workspace/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
            state['stages'][stage]={'workspace':str(workspace),'status':'ready'}
        state['root_discovery']=reference(root/'train','train',root_probe=True)
        state['initial_reference']=reference(root/'train','train')
        self.save(state)
        return self.status()

    def preview(self,stage: str) -> dict:
        state=self.current()
        if stage not in state['stages']: raise ValueError('Unknown lab stage')
        if state['stages'][stage]['status']!='ready': raise ValueError('Stage already attempted; keep its result or create a new lab')
        if stage!='train' and not state['memory_id']: raise ValueError('Train, review and accept the project memory first')
        memory=None
        if state['memory_id']:
            m=Memory(self.store).get(self.owner,state['primary_project'],state['memory_id'])
            if m['status']!='active' or not m['verified']: raise ValueError('Lab memory is no longer active')
            if stage=='memory': memory={k:m[k] for k in ('id','revision','content','scope','project')}
        target=('修复折扣计算，使 percent=20 表示20%折扣。' if stage=='train' else
                '修复 slug，使结果去除首尾空白、使用小写并以短横线替换空格。' if stage=='other' else
                '修复分页函数，页码从1开始。')
        packet={'lab_id':state['id'],'stage':stage,'workspace':state['stages'][stage]['workspace'],
                'task':target+'先检查项目，修复后运行实际测试，报告结果。不要修改测试和配置。',
                'rules':RULES,'coding':state['coding']['payload']['instructions'],'memory':memory,
                'initial_fingerprint':snapshot(state['stages'][stage]['workspace'])['manifest_hash'],
                'files':{name:(Path(state['stages'][stage]['workspace'])/name).read_text(encoding='utf-8')
                         for name in files_for(stage)},'thinking':'off','timeout_seconds':105}
        return {'lab_preview':packet,'packet_hash':digest(packet)}

    def review(self) -> dict:
        state=self.current();training=state['stages']['train']
        if (training.get('status')!='completed' or training.get('verification',{}).get('status')!='passed'
                or not training.get('agent_verified') or training.get('root_probe',{}).get('status')!='no_tests'):
            raise ValueError('Training agent must actually run nonzero tests, and the independent reference must pass')
        reflection=Reflection(self.store,self.owner,state['primary_project'])
        prepared=reflection.prepare(training['task_id'])
        ref=training['verification_evidence']
        root_ref=training['root_probe_evidence']
        if not any(e['id']==root_ref for e in prepared['packet']['evidence']):
            prepared['packet']['evidence'].append({'id':root_ref,'kind':'verification',
                'data':{'command':'python -m unittest -v','status':'no_tests','test_count':0}})
            prepared['packet_hash']=digest(prepared['packet'])
        proposals=[{'title':'本项目的实际测试入口','kind':'project_memory','basis':'observation',
                    'content':'此项目应从项目根目录运行 python checks/run_tests.py；根目录默认 unittest 可能发现0个测试，应核对实际执行的测试数量。',
                    'evidence_ids':[root_ref,ref]}]
        candidates=reflection.save(prepared,proposals,'lab-reference-extraction')
        state['candidate_id']=candidates[0]['id'];self.save(state)
        return {'lab':self.status()['lab'],'candidate':candidates[0]}

    def accept(self,candidate: str,revision: int) -> dict:
        state=self.current()
        if state.get('candidate_id')!=candidate: raise ValueError('Candidate does not belong to the current lab')
        c=Reflection(self.store,self.owner,state['primary_project']).decide(candidate,True,revision)
        state['memory_id']=c['memory_id'];self.save(state)
        return self.status()

    def candidate(self) -> dict:
        state=self.current()
        if not state.get('candidate_id'): raise ValueError('Run /harness lab review first')
        return Reflection(self.store,self.owner,state['primary_project']).get(state['candidate_id'])

    def edit(self,content: str,revision: int) -> dict:
        state=self.current();candidate=self.candidate()
        return {'lab':state,'candidate':Reflection(self.store,self.owner,state['primary_project']).edit(
                    candidate['id'],content,revision)}

    def status(self) -> dict:
        state=self.current();isolation={'status':'not_checked'}
        if state['memory_id']:
            memory=Memory(self.store)
            primary=memory.retrieve(self.owner,state['primary_project'],'项目测试入口')
            other=memory.retrieve(self.owner,state['other_project'],'项目测试入口')
            isolation={'status':'passed' if any(m['id']==state['memory_id'] for m in primary) and
                       not any(m['id']==state['memory_id'] for m in other) else 'failed',
                       'method':'Actual scoped memory retrieval; not a behavioral generalization test'}
        a,b=state['stages']['baseline'],state['stages']['memory']
        comparison={'outcome':'not_run'}
        if all(s.get('status') in {'completed','failed'} for s in (a,b)):
            if any(s.get('status')!='completed' for s in (a,b)):
                comparison={'outcome':'inconclusive','reason':'One or both model runs failed'}
            elif a.get('model')!=b.get('model'):
                comparison={'outcome':'inconclusive','reason':'Models differ between arms'}
            elif a.get('initial_fingerprint')!=b.get('initial_fingerprint'):
                comparison={'outcome':'inconclusive','reason':'Initial holdout code differs between arms'}
            elif any(s.get('verification',{}).get('status') in {'protected_files_changed','timeout','code_changed_during_test'} for s in (a,b)):
                comparison={'outcome':'inconclusive','reason':'Invalid reference evaluation'}
            else:
                x=a.get('verification',{}).get('status')=='passed';y=b.get('verification',{}).get('status')=='passed'
                reduction=a.get('zero_test_calls',0)-b.get('zero_test_calls',0)
                comparison={'outcome':'improved_on_this_pair' if y and (not x or reduction>0) else 'worse_on_this_pair' if x and (not y or reduction<0) else 'tie_on_this_pair',
                    'baseline_passed':x,'memory_passed':y,
                    'zero_test_calls_reduced':reduction}
        pending=[stage for stage in state['pair_order'] if state['stages'][stage]['status']=='ready']
        return {'lab':state,'isolation':isolation,'comparison':comparison,
                'next_stage':pending[0] if state['memory_id'] and pending else None}

    async def run(self,preview: dict,pi_root: str,provider: str,model: str) -> dict:
        from .native import native_tools
        packet=preview['lab_preview'];state=self.current();stage=packet['stage'];entry=state['stages'][stage]
        logical_project=state['other_project'] if stage=='other' else state['primary_project']
        task=self.store.task(self.owner,logical_project,packet['task'])
        evidence=Evidence(self.store,self.owner,logical_project)
        evidence.record(task,'context_assembled',{'workspace':packet['workspace'],'interface':'cross-task-lab',
            'stage':stage,'memory_ids':[packet['memory']['id']] if packet['memory'] else [],
            'memory_context':[packet['memory']] if packet['memory'] else []})
        evidence.capture(task,'begin')
        if stage=='train':
            root_probe=reference(Path(packet['workspace']),stage,root_probe=True)
            entry['root_probe']=root_probe
            entry['root_probe_evidence']=evidence.record(task,'verification',root_probe)
        entry.update(status='running',task_id=task,model={'provider':provider,'id':model},
                     initial_fingerprint=packet['initial_fingerprint'])
        state['attempted_order'].append(stage);self.save(state)
        command=pi_command(pi_root,tools=True,provider=provider,model=model)
        command.extend(['--tools',native_tools(True),'--thinking','off'])
        prompt=packet['coding']+'\n'+packet['rules']+'\n'+packet['task']
        if packet['memory']: prompt+='\nScoped project memory:\n'+packet['memory']['content']
        observations=[]
        def record(event):
            if event.get('type') not in {'tool_execution_start','tool_execution_end'}: return
            result=event.get('result') or {}
            output='\n'.join(b.get('text','') for b in result.get('content',[]) if b.get('type')=='text')[:12000]
            data=redact({'phase':'start' if event['type'].endswith('start') else 'end',
                'toolName':event.get('toolName'),'toolCallId':event.get('toolCallId'),
                'args':event.get('args'),'output':output,'isError':event.get('isError',False)})
            observations.append(data);evidence.record(task,'native_tool',data)
        text,error='',None
        try:
            async with asyncio.timeout(105):
                async with PiRpc(command,packet['workspace'],90,record) as rpc:
                    await rpc.request('set_auto_retry',enabled=False)
                    selected=(await rpc.request('get_state')).get('model') or {}
                    if (selected.get('provider'),selected.get('id'))!=(provider,model): raise ValueError('Confirmed model mismatch')
                    text=(await rpc.prompt(prompt))['text']
        except Exception as exc: error=redact(str(exc)) or type(exc).__name__
        verification=reference(Path(packet['workspace']),stage)
        ref=evidence.record(task,'verification',verification)
        evidence.capture(task,'finish')
        self.store.finish_task(task,'failed' if error else 'completed',redact(text or error))
        entry.update(status='failed' if error else 'completed',error=error,text=redact(text),
                     verification=verification,verification_evidence=ref,
                     agent_verified=any(re.search(r'Ran [1-9]\d* tests?\b',e['output']) and not e['isError']
                                        for e in observations if e['phase']=='end' and e['toolName'] in {'bash','powershell'}),
                     zero_test_calls=sum(bool(re.search(r'Ran 0 tests?\b',e['output'])) for e in observations
                                         if e['phase']=='end' and e['toolName'] in {'bash','powershell'}))
        self.save(state)
        return self.status()
