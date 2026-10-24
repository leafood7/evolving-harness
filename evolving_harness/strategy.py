"""Finite, inspectable reviewer evolution; no arbitrary evaluator/code mutation."""
from .components import Components, CHECKS
from .domain import digest

MESSAGES={
 'scope':'Check project and task applicability before generalizing an experience; never expand scope without evidence.',
 'expiry':'Check whether the memory is stale or contradicted by current configuration and actual execution.',
 'duplicate':'Check whether the memory adds information beyond existing memories/project instructions; avoid duplicate candidates.',
 'test_count':'Check actual executed test counts; a zero-test run or fewer tool calls is not evidence of success.',
}


def inspect(payload, observation):
    """The same active rules govern local review and compiled LLM guidance."""
    findings=[]
    for name in payload['checks']:
        if name=='scope' and observation.get('memory_project') not in (None,observation.get('task_project')):
            findings.append('scope')
        if name=='expiry' and observation.get('expired') is True: findings.append('expiry')
        if name=='duplicate' and observation.get('duplicate') is True: findings.append('duplicate')
        if name=='test_count' and observation.get('claimed_pass') is True and observation.get('test_count')==0:
            findings.append('test_count')
    return findings


def compile_strategy(component):
    return '\n'.join(MESSAGES[k] for k in component['payload']['checks'])


class Strategy:
    def __init__(self,store,owner,project):
        self.store,self.owner,self.project=store,owner,project
        self.components=Components(store,owner,project)

    def feedback(self,check,note):
        if check not in CHECKS or not isinstance(note,str) or not note.strip():
            raise ValueError('Feedback requires scope/expiry/duplicate/test_count and a reason')
        return self.store.event(self.owner,self.project,'reviewer_feedback',{'check':check,'note':note[:2000]})

    def preview(self):
        current=self.components.seed('reviewer')
        feedback=self.store.events(self.owner,self.project,'reviewer_feedback')[-50:]
        candidates=[c for c in self.components.history() if c['name']=='reviewer']
        return {'active':current,'feedback':[{'id':e['seq'],**e['data']} for e in feedback],
                'previous_candidates':[{'id':c['id'],'hash':c['hash'],'generator':c['generator']} for c in candidates],
                'notice':'Bounded rule adaptation; independent fixed synthetic gate is not a real-model gain benchmark.'}

    def propose(self):
        p=self.preview();current=p['active'];payload=current['payload']
        # Current strategy determines which observed defect gets the next experiment.
        present={e['check'] for e in p['feedback']}
        missing=next((k for k in payload['priority'] if k in present and k not in payload['checks']),None)
        if missing is None: raise ValueError('No new supported defect; no-op upgrades are not created')
        target=payload|{'checks':payload['checks']+[missing]}
        failed={e['data'].get('payload_hash') for e in self.store.events(self.owner,self.project,'component_failed')}
        if digest(target) in failed: raise ValueError('This candidate already failed; supply a different strategy proposal')
        existing=next((c for c in self.components.history() if c['name']=='reviewer' and
                       c['hash']==digest(target) and c['parent']==current['id']),None)
        if existing:return existing
        return self.components.propose('reviewer',target,[e['id'] for e in p['feedback'] if e['check']==missing],
            backend='bounded-feedback-planner',reason='Address observed reviewer defect: '+missing,generator=current['id'])

    def observe_task(self,task):
        """Metadata/tool facts only; never infer semantic failure from model prose."""
        import re
        self.store.get_task(self.owner,self.project,task)
        previous={(e['data'].get('source_event'),e['data']['check']) for e in
                  self.store.events(self.owner,self.project,'reviewer_feedback')}
        findings=[]
        for event in self.store.events(self.owner,self.project):
            if event['task_id']!=task:continue
            data=event['data'];check=None
            if event['kind']=='feedback' and data.get('source')=='memory-audit':
                check={'duplicate':'duplicate','expired':'expiry'}.get(data.get('defect'))
            elif event['kind']=='native_tool' and data.get('phase')=='end' and re.search(r'Ran 0 tests?\b',data.get('output','')):
                check='test_count'
            if check and (event['seq'],check) not in previous:
                seq=self.store.event(self.owner,self.project,'reviewer_feedback',{'check':check,
                    'note':'Observed metadata/tool condition; not a causal benefit claim',
                    'source':'automatic-local-observation','source_event':event['seq']},task)
                findings.append(seq)
        candidate=None
        if findings:
            try:candidate=self.propose()['id']
            except ValueError:pass
        return {'feedback_ids':findings,'pending_candidate':candidate,'notice':'Local draft only; activation still requires evaluation and confirmation.'}

    def review_observations(self, observations):
        c=self.components.seed('reviewer')
        result=[{'observation':o,'findings':inspect(c['payload'],o)} for o in observations]
        self.store.event(self.owner,self.project,'reviewer_used',{'strategy_id':c['id'],'version':c['version'],
                         'observations':result})
        return {'strategy':c,'results':result}
