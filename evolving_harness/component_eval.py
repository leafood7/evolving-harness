"""Independent fixed contract/regression gates, outside generated payload vocabulary."""
from .domain import digest

REVIEW_CASES=[
 ({'memory_project':'a','task_project':'b'},['scope']),
 ({'expired':True},['expiry']),
 ({'duplicate':True},['duplicate']),
 ({'claimed_pass':True,'test_count':0},['test_count']),
 ({'memory_project':'a','task_project':'a','test_count':2,'claimed_pass':True},[]),
 ({'expired':False,'duplicate':False,'test_count':3,'claimed_pass':True},[]),
]


def evaluate(baseline,candidate):
    from .components import validate
    validate(candidate['name'],candidate['payload'])
    payload=candidate['payload'];kind=payload['kind']
    changed=baseline is None or baseline['hash']!=candidate['hash']
    report={'decision':'reject','kind':kind,'suite_hash':digest(REVIEW_CASES if kind=='reviewer' else ['contract-v1',kind]),
            'claim':'Fixed local contract checks, not statistical real-model performance.'}
    if kind=='reviewer':
        from .strategy import inspect
        old=[inspect(baseline['payload'],x)==y for x,y in REVIEW_CASES] if baseline else [False]*len(REVIEW_CASES)
        new=[inspect(payload,x)==y for x,y in REVIEW_CASES]
        report.update(baseline_correct=sum(old),candidate_correct=sum(new),cases=len(new),
                      no_regression=all(not a or b for a,b in zip(old,new)))
        accepted=changed and sum(new)>sum(old) and report['no_regression']
    elif kind=='context':
        from .personalization import budget_memories
        fixture=[{'id':'a','content':'important test command'},{'id':'b','content':'important test command'},
                 {'id':'c','content':'x'*22000}]
        selected=budget_memories(fixture,payload)
        accepted=changed and selected and selected[0]['id']=='a' and sum(len(m['content']) for m in selected)<=payload['max_chars']
        report['contract']='Relevant first memory retained; whole-memory budget and dedup tested.'
    else:
        accepted=changed
        report['contract']='Schema/adapter validation only; requires explicit user review. No automatic coding-gain claim.'
    report['decision']='accept' if accepted else 'reject'
    return report
