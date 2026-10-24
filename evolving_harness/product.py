"""Native control-plane operations for versioned Harness features."""
from .components import Components,SEEDS
from .domain import digest
from .strategy import Strategy
from .runtime import redact


def handle(store,owner,project,request):
    op=request['operation'];scope='@user' if request.get('user_scope') else project
    registry=Components(store,owner,scope)
    if op.startswith('product-skill-compare-'):
        from .skill_compare import SkillCompare
        trial=SkillCompare(store,owner,project)
        if op=='product-skill-compare-setup':return trial.setup(request['workspace'])
        if op=='product-skill-compare-status':return {'skill_comparison_result':trial.current()}
        if op=='product-skill-compare-run':
            if request.get('confirmed') is not True or not request.get('provider') or not request.get('model'):
                raise ValueError('Confirm both real model runs, outbound code and shell access')
            if __import__('os').environ.get('EH_ALLOW_WRITE')!='1':raise ValueError('Restart native with --allow-write for real-code comparison')
            p=trial.preview()
            if request.get('packet_hash')!=p['packet_hash']:raise ValueError('Comparison preview changed')
            import asyncio
            return asyncio.run(trial.run(p,request['pi_root'],request['provider'],request['model']))
        raise ValueError('Unknown Skill comparison operation')
    if op.startswith('product-improve-'):
        from .improvement_batch import ImprovementBatch
        batch=ImprovementBatch(store,owner,project)
        if op=='product-improve-plan':return batch.plan()
        if op=='product-improve-status':return batch.status()
        if op=='product-improve-activate':
            if request.get('confirmed') is not True:raise ValueError('Confirm this evaluated batch before activation')
            return batch.activate(request['id'])
        raise ValueError('Unknown batch operation')
    if op=='product-demo':
        return recursive_demo()
    if op in {'product-benchmark-preview','product-benchmark-run'}:
        from .review_benchmark import prepare,run
        p=prepare(store,owner,project,request['id'])
        if op.endswith('preview'): return p
        if request.get('confirmed') is not True or not request.get('provider') or not request.get('model'):
            raise ValueError('Confirm benchmark payload and selected model')
        if request.get('packet_hash')!=p['packet_hash']: raise ValueError('Benchmark changed after preview')
        import asyncio
        return asyncio.run(run(store,owner,project,p,request['pi_root'],request['provider'],request['model']))
    if op=='product-status':
        for name in ('reviewer','context'): registry.seed(name)
        profile=Components(store,owner,'@user').seed('profile')
        history=registry.history()
        evaluations=[dict(r) for r in store.db.execute('SELECT id,candidate,report FROM component_evaluations WHERE owner=? AND project=? ORDER BY rowid DESC LIMIT 30',(owner,project))]
        import json
        return {'product':{'project':project,'user':owner,'active':[c for c in history if (registry.active(c['name']) or {}).get('id')==c['id']],
            'profile':profile,'history':history[-50:],'evaluations':[e|{'report':json.loads(e['report'])} for e in evaluations],
            'notice':'Local user scope, not network authentication. Contract gates do not establish real coding gains.'}}
    if op=='component-template':
        name=request['name'];active=registry.active(name)
        payload=(active or {}).get('payload') or SEEDS.get(name)
        if payload is None:
            if name.startswith('skill:'): payload={'kind':'skill','description':'Project workflow','triggers':['修复','fix'],'steps':['Inspect project instructions and reproduce the defect.','Make the smallest relevant change.','Run the actual project tests and report executed test count.']}
            elif name.startswith('tool:'): payload={'kind':'tool','description':'Inspect bounded workspace files','recipe':'workspace_summary','timeout':15}
            else: raise ValueError('Unknown component name')
        return {'component_template':{'name':name,'payload':payload,'reason':'Explain the intended change and evidence here','evidence_ids':[]},'user_scope':scope=='@user'}
    if op=='component-stage':
        d=request['draft']
        if not isinstance(d,dict) or set(d)!={'name','payload','reason','evidence_ids'}: raise ValueError('Invalid draft schema')
        if d['payload'].get('kind')=='profile' and scope!='@user': raise ValueError('Preferences use explicit user scope')
        return {'component':registry.propose(d['name'],redact(d['payload']),d['evidence_ids'],reason=redact(d['reason']))}
    if op=='component-get': return {'component':registry.get(request['id'])}
    if op=='component-evaluate':
        report=registry.evaluate(request['id'])
        if report['decision']=='reject':
            store.event(owner,scope,'component_failed',{'id':request['id'],'payload_hash':registry.get(request['id'])['hash']})
        return {'component_evaluation':report}
    if op=='component-activate':
        if request.get('confirmed') is not True: raise ValueError('Explicit component approval required')
        return {'component':registry.promote(request['id'],request['evaluation_id'])}
    if op=='component-rollback':
        if request.get('confirmed') is not True: raise ValueError('Explicit rollback approval required')
        return {'component':registry.rollback(request['name'])}
    if op=='component-disable':
        if request.get('confirmed') is not True: raise ValueError('Explicit disable approval required')
        return registry.disable(request['name'])
    if op=='component-export': return registry.export_skill(request['id'])
    if op=='component-last-error':
        events=store.events(owner,project,'component_proposal_outcome')
        return {'component_failure':events[-1]['data'] if events else {'status':'no_record','error':'No diagnostic saved; older failed requests cannot be reconstructed.'}}
    if op=='strategy-feedback': return {'feedback_id':Strategy(store,owner,project).feedback(request['check'],request['note'])}
    if op=='strategy-propose': return {'component':Strategy(store,owner,project).propose()}
    if op=='strategy-preview': return {'strategy':Strategy(store,owner,project).preview()}
    if op in {'component-preview','component-model'}:
        from .component_model import prepare,generate,ProposalFailure
        preview=prepare(store,owner,project,request['name'])
        if op=='component-preview': return preview
        if request.get('confirmed') is not True or not request.get('provider') or not request.get('model'):
            raise ValueError('Confirm outbound payload and model first')
        if request.get('packet_hash')!=preview['packet_hash']: raise ValueError('Component preview changed')
        import asyncio
        try:
            return {'component':asyncio.run(generate(store,owner,project,preview,request['pi_root'],request['provider'],request['model']))}
        except ProposalFailure as exc:
            return {'component_failure':exc.diagnostic}
    if op in {'tool-preview','tool-run'}:
        from .managed_tools import preview,run
        p=preview(store,owner,project,request['name'],request['workspace'])
        if op=='tool-preview': return p
        if request.get('confirmed') is not True or request.get('packet_hash')!=p['packet_hash']:
            raise ValueError('Confirm the current tool command and workspace first')
        return {'tool_result':run(store,owner,project,p)}
    if op=='skill-from-experience':
        from .reflection import Reflection
        c=Reflection(store,owner,project).get(request['id'])
        if c['kind']!='workflow' or c['status']!='reviewed_workflow': raise ValueError('Accept the workflow experience first')
        payload={'kind':'skill','description':c['title'],'triggers':request['triggers'],
                 'steps':[c['content'],'Run relevant verification and report evidence; never claim unexecuted tests.']}
        return {'component':registry.propose(request['name'],payload,c['evidence_ids'],backend='accepted-workflow',reason='Convert reviewed workflow; contract check and approval still required')}
    if op=='experiments-status':
        feedback=store.events(owner,project,'reviewer_feedback')
        active=registry.seed('reviewer')
        pending=[k for k in active['payload']['priority'] if k not in active['payload']['checks'] and any(e['data']['check']==k for e in feedback)]
        return {'experiments':{'next_checks':pending,'budget':'One candidate per confirmation, model timeout 130s; no unattended model loop.',
                              'attempts':len(registry.history()),'failed_payloads':[e['data'] for e in store.events(owner,project,'component_failed')],
                              'method':'Current reviewer priorities; identical model proposals rejected; fixed suite, no causal gain claim.'}}
    raise ValueError('Unknown product operation')


def recursive_demo():
    """Isolated deterministic fixture. Never masquerades as autonomous model improvement."""
    import tempfile
    from pathlib import Path
    from .store import Store
    from .memory import Memory
    from .revision_model import prepare
    with tempfile.TemporaryDirectory(prefix='harness-recursion-') as folder:
        s=Store(Path(folder)/'state.db')
        try:
            strategy=Strategy(s,'fixture','recursion');manager=strategy.components
            initial=manager.seed('reviewer');rounds=[]
            for check in ('scope','expiry'):
                previous=manager.seed('reviewer')
                strategy.feedback(check,'Explicit offline fixture defect, not live user feedback')
                candidate=strategy.propose();gate=manager.evaluate(candidate['id'])
                assert gate['decision']=='accept' and candidate['generator']==previous['id']
                manager.promote(candidate['id'],gate['evaluation_id'])
                used=strategy.review_observations([{'memory_project':'x','task_project':'y','expired':True}])
                assert used['strategy']['id']==candidate['id']
                rounds.append({'id':candidate['id'],'version':candidate['version'],'generator':candidate['generator'],
                               'decision':gate['decision'],'local_findings':used['results'][0]['findings']})
            mid=Memory(s).add('fixture','recursion','Use project test command','offline fixture',verified=True)
            prepared=prepare(s,'fixture','recursion',mid)
            assert prepared['revision_preview']['strategy']['id']==rounds[-1]['id']
            return {'recursive_demo':{'rounds':rounds,'seed':initial['id'],
                'next_memory_review_generator':prepared['revision_preview']['strategy']['id'],
                'notice':'Deterministic bounded-rule recursion: v2 generates v3; v3 is loaded in memory-review guidance. No model calls, no real-world gain claim.'}}
        finally:s.close()
