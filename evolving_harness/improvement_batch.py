"""One local batch: collect facts, propose, evaluate, approve, track actual follow-up."""
from .components import Components
from .domain import uid,now
from .strategy import Strategy


class ImprovementBatch:
    def __init__(self,store,owner,project):
        self.store,self.owner,self.project=store,owner,project
        self.registry=Components(store,owner,project)

    def save(self,report):
        self.store.event(self.owner,self.project,'improvement_batch',report)
        return {'improvement_batch':report}

    def get(self,key=None):
        reports=self.store.events(self.owner,self.project,'improvement_batch')
        found=next((e['data'] for e in reversed(reports) if not key or e['data']['id']==key),None)
        if not found:raise ValueError('No improvement batch in this user/project')
        return found

    def plan(self):
        strategy=Strategy(self.store,self.owner,self.project)
        current=self.registry.seed('reviewer')
        tasks=list(dict.fromkeys(e['task_id'] for e in self.store.events(self.owner,self.project,'native_settled') if e['task_id']))[-20:]
        observations=[]
        for task in tasks:
            if self.store.get_task(self.owner,self.project,task)['state']=='completed':
                observations.append(strategy.observe_task(task))
        report={'id':uid('batch'),'created':now(),'baseline':current['id'],'baseline_version':current['version'],
            'tasks_checked':len(tasks),'new_feedback':sum(len(o['feedback_ids']) for o in observations),
            'candidate':None,'evaluation':None,'status':'no_change',
            'reason':'没有新的受支持缺陷证据；保持当前策略，不调用模型。',
            'limits':'有限本地规则和固定案例门槛；不代表真实模型能力提升。'}
        try:candidate=strategy.propose()
        except ValueError as exc:
            report['detail']=str(exc)
            return self.save(report)
        gate=self.registry.evaluate(candidate['id'])
        report.update(candidate=candidate['id'],candidate_version=candidate['version'],
                      generator=candidate['generator'],evidence_ids=candidate['evidence'],payload=candidate['payload'],
                      evaluation=gate,status='ready' if gate['decision']=='accept' else 'rejected',
                      reason=candidate['reason'])
        return self.save(report)

    def activate(self,key):
        report=self.get(key)
        if report['status']!='ready':raise ValueError('Batch is not awaiting approval or was already activated')
        candidate=self.registry.promote(report['candidate'],report['evaluation']['evaluation_id'])
        # Verify persisted active pointer, not a fabricated future model run.
        assert self.registry.active('reviewer')['id']==candidate['id']
        report=report|{'status':'awaiting_followup','activated':now(),
                       'reason':'新策略已启用；等待真实后续任务和模型复盘使用记录。'}
        return self.save(report)

    def status(self):
        report=self.get()
        follow=[e['data'] for e in self.store.events(self.owner,self.project,'improvement_followup') if e['data']['batch']==report['id']]
        return {'improvement_batch':report|{'followup':follow}}


def track(store,owner,project,strategy_id,kind,reference):
    """Configuration receipt and actual model-review receipt remain distinct."""
    events=store.events(owner,project,'improvement_batch')
    if not events:return
    seen=set();report=None
    for event in reversed(events):
        value=event['data']
        if value['id'] in seen:continue
        seen.add(value['id'])
        if value['status']=='awaiting_followup' and value['candidate']==strategy_id:
            report=value;break
    if report is None:return
    if kind not in {'task_context','model_review'}:raise ValueError('Unknown follow-up kind')
    previous=store.events(owner,project,'improvement_followup')
    if any(e['data'].get('batch')==report['id'] and e['data'].get('kind')==kind and e['data'].get('reference')==reference for e in previous):return
    store.event(owner,project,'improvement_followup',{'batch':report['id'],'strategy':strategy_id,'kind':kind,
        'reference':reference,'notice':'Version use receipt, not a causal improvement measurement.'})
