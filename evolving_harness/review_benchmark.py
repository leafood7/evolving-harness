"""Small synthetic held-out model comparison. Labels never enter generation prompts."""
import asyncio
import json
import tempfile

from .components import Components
from .domain import canonical,digest
from .revision_model import GUIDANCE
from .rpc import PiRpc,pi_command
from .runtime import redact
from .strategy import compile_strategy

CASES=[
 {'name':'stale_entry','memory':'Run python legacy_tests.py and check test count.',
  'evidence':[{'id':1,'text':'python legacy_tests.py: No such file or directory; exit 2'},
              {'id':2,'text':'project.toml: command=python checks/run_tests.py; actual execution Ran 2 tests, OK'}]},
 {'name':'healthy_tie','memory':'Run python checks/run_tests.py and check test count.',
  'evidence':[{'id':1,'text':'Current project.toml agrees; actual execution Ran 2 tests, OK'},
              {'id':2,'text':'With and without memory both passed; no repeated zero-test calls in either arm.'}]},
 {'name':'zero_tests','memory':'The project is verified because python -m unittest returned success.',
  'evidence':[{'id':1,'text':'Actual captured output: Ran 0 tests; no tests were executed.'},
              {'id':2,'text':'No other verification result exists for this task.'}]},
]


def prepare(store,owner,project,key):
    registry=Components(store,owner,project);candidate=registry.get(key)
    if candidate['payload']['kind']!='reviewer' or not candidate['parent']:
        raise ValueError('Benchmark requires a reviewer candidate with a parent')
    baseline=registry.get(candidate['parent'])
    p={'baseline':baseline,'candidate':candidate,'cases':CASES,
       'requests':6,'limit_seconds':240,'notice':'Fixed synthetic memory-review cases, independent sessions, no project code; results may tie or fail.'}
    return {'benchmark_preview':p,'packet_hash':digest(p)}


def score(case,text):
    try:
        p=json.loads(text)
        if not isinstance(p,dict) or set(p)!={'action','content','reason','evidence_ids'}: raise ValueError()
        if not all(isinstance(p[k],str) and 0<len(p[k])<=2000 for k in ('action','content','reason')): raise ValueError()
        if p['action'] not in {'keep','revise','retire'}: raise ValueError()
        refs=p['evidence_ids']
        if not isinstance(refs,list) or not refs or any(type(i) is not int or i not in {1,2} for i in refs): raise ValueError()
        if case['name']=='stale_entry':
            correct=p['action']=='revise' and 'checks/run_tests.py' in p['content'] and 'legacy_tests.py' not in p['content']
        elif case['name']=='healthy_tie':
            correct=p['action']=='keep' and p['content']==case['memory']
        else:
            if p['action']=='retire': correct=p['content']==case['memory']
            else:
                lower=p['content'].casefold()
                correct=(p['action']=='revise' and p['content']!=case['memory'] and
                         ('test' in lower or '测试' in lower) and any(t in lower for t in ('count','zero','0','数量')))
        return {'valid':True,'correct':bool(correct),'action':p['action']}
    except (ValueError,TypeError): return {'valid':False,'correct':False,'action':None}


async def run(store,owner,project,prepared,pi_root,provider,model):
    p=prepared['benchmark_preview'];results=[]
    command=pi_command(pi_root,tools=False,provider=provider,model=model)+['--thinking','off']
    try:
        async with asyncio.timeout(240):
            for index,case in enumerate(p['cases']):
                # Counterbalance ordering; never select the better sample after repeated retries.
                for arm in (('baseline','candidate') if index%2==0 else ('candidate','baseline')):
                    prompt=GUIDANCE+'\nVersioned checks:\n'+compile_strategy(p[arm])+'\nEvidence JSON:\n'+canonical(case)
                    try:
                        with tempfile.TemporaryDirectory(prefix='review-benchmark-') as folder:
                            async with PiRpc(command,folder,30) as rpc:
                                await rpc.request('set_auto_retry',enabled=False)
                                selected=(await rpc.request('get_state')).get('model') or {}
                                if (selected.get('provider'),selected.get('id'))!=(provider,model): raise ValueError('Model mismatch')
                                response=await rpc.prompt(prompt)
                        text=redact(response['text']);metrics=score(case,text)
                        results.append({'arm':arm,'case':case['name'],'status':'completed','text':text,'metrics':metrics})
                    except Exception as exc:
                        results.append({'arm':arm,'case':case['name'],'status':'failed','error':redact(str(exc))})
    except TimeoutError:
        pass
    complete=len(results)==6 and all(r['status']=='completed' and r['metrics']['valid'] for r in results)
    totals={arm:sum(r.get('metrics',{}).get('correct',False) for r in results if r['arm']==arm) for arm in ('baseline','candidate')}
    outcome=('improved_on_this_suite' if totals['candidate']>totals['baseline'] else
             'worse_on_this_suite' if totals['candidate']<totals['baseline'] else 'tie_on_this_suite') if complete else 'inconclusive'
    report={'benchmark_result':{'outcome':outcome,'correct':totals,'results':results,
        'baseline':p['baseline']['id'],'candidate':p['candidate']['id'],'model':{'provider':provider,'id':model},
        'packet_hash':prepared['packet_hash'],
        'limitations':'One sample per case/arm, synthetic fixtures and simple content checks; not semantic proof, statistical evidence or an automatic promotion gate.'}}
    store.event(owner,project,'reviewer_benchmark',report['benchmark_result'])
    return report
