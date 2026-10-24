import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evolving_harness.store import Store
from evolving_harness.native import dispatch
from evolving_harness.components import Components
from evolving_harness.strategy import Strategy
from evolving_harness.memory import Memory
from evolving_harness.memory_revision import Revisions
from evolving_harness.personalization import assemble
from evolving_harness.component_model import prepare,save


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'state.db')
        self.c=Components(self.store,'alice','project')
        dispatch(self.store,'alice','project',{'operation':'status'})

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def call(self,operation,**kw):
        return dispatch(self.store,'alice','project',{'operation':operation,**kw})

    def install(self,name,payload,manager=None):
        c=manager or self.c
        candidate=c.propose(name,payload,[],reason='explicit fixture configuration')
        report=c.evaluate(candidate['id']);self.assertEqual(report['decision'],'accept')
        c.promote(candidate['id'],report['evaluation_id'])
        return candidate

    def test_recursive_successor_uses_new_strategy_and_real_review_guidance(self):
        from evolving_harness.product import recursive_demo
        result=recursive_demo()['recursive_demo'];a,b=result['rounds']
        self.assertEqual(a['generator'],result['seed'])
        self.assertEqual(b['generator'],a['id'])
        self.assertEqual(result['next_memory_review_generator'],b['id'])
        self.assertIn('expiry',b['local_findings'])

    def test_strategy_noop_and_regression_cannot_be_promoted(self):
        s=Strategy(self.store,'alice','project');base=self.c.seed('reviewer')
        with self.assertRaises(ValueError):s.propose()
        s.feedback('scope','fixture issue');new=s.propose();gate=self.c.evaluate(new['id'])
        self.c.promote(new['id'],gate['evaluation_id'])
        regression=self.c.propose('reviewer',base['payload'],[],reason='regression fixture')
        bad=self.c.evaluate(regression['id']);self.assertEqual(bad['decision'],'reject')
        with self.assertRaises(ValueError):self.c.promote(regression['id'],bad['evaluation_id'])
        self.assertEqual(self.c.rollback('reviewer')['id'],base['id'])

    def test_stale_baseline_and_wrong_evaluation_and_scope_are_rejected(self):
        self.c.seed('context')
        a=self.c.propose('context',{'kind':'context','max_memories':2,'max_chars':1000,'deduplicate':True},[],reason='a')
        b=self.c.propose('context',{'kind':'context','max_memories':3,'max_chars':1000,'deduplicate':True},[],reason='b')
        ga=self.c.evaluate(a['id']);gb=self.c.evaluate(b['id'])
        with self.assertRaises(ValueError):self.c.promote(a['id'],gb['evaluation_id'])
        self.c.promote(a['id'],ga['evaluation_id'])
        with self.assertRaises(ValueError):self.c.promote(b['id'],gb['evaluation_id'])
        with self.assertRaises(ValueError):Components(self.store,'bob','project').get(a['id'])
        with self.assertRaises(ValueError):Components(self.store,'alice','other').get(a['id'])

    def test_profile_shared_across_owners_projects_but_not_other_users(self):
        profile=Components(self.store,'alice','@user');profile.seed('profile')
        self.install('profile',{'kind':'profile','preferences':['Prefer small diffs and Chinese explanations.']},profile)
        a=assemble(self.store,'alice','project','fix',[])['instructions']
        b=assemble(self.store,'alice','other','fix',[])['instructions']
        other=assemble(self.store,'bob','project','fix',[])['instructions']
        self.assertIn('Prefer small diffs',a);self.assertIn('Prefer small diffs',b)
        self.assertNotIn('Prefer small diffs',other)

    def test_context_budget_dedup_and_used_versions_are_recorded(self):
        self.c.seed('context')
        c=self.install('context',{'kind':'context','max_memories':2,'max_chars':256,'deduplicate':True})
        for content in ('same','same','x'*300): Memory(self.store).add('alice','project',content,'fixture',verified=True)
        result=self.call('begin',prompt='same',workspace=self.temp.name)
        self.assertEqual(len(result['memories']),1)
        event=self.store.events('alice','project','context_assembled')[-1]['data']
        self.assertEqual(event['components']['context'],c['id'])
        self.assertLessEqual(event['context_metrics']['memory_chars'],256)

    def test_skill_activation_trigger_export_revision_and_disable(self):
        p={'kind':'skill','description':'Repair workflow','triggers':['repair'],
           'steps':['Inspect the failing test.','Run the actual project command and count tests.']}
        c=self.install('skill:repair',p)
        yes=assemble(self.store,'alice','project','repair pagination',[])
        no=assemble(self.store,'alice','project','explain architecture',[])
        self.assertEqual(yes['components']['skills'],[c['id']]);self.assertEqual(no['components']['skills'],[])
        path=Path(self.c.export_skill(c['id'])['path'])
        self.assertIn('name: repair',path.read_text(encoding='utf-8'))
        successor=self.install('skill:repair',p|{'steps':p['steps']+['Report remaining uncertainty.']})
        self.assertEqual(self.c.rollback('skill:repair')['id'],c['id'])
        self.c.disable('skill:repair')
        self.assertEqual(assemble(self.store,'alice','project','repair',[])['components']['skills'],[])

    def test_tool_no_shell_payload_and_explicit_execution_confirmation(self):
        p={'kind':'tool','description':'Inspect workspace','recipe':'workspace_summary','timeout':3}
        with self.assertRaises(ValueError):self.c.propose('tool:bad',p|{'recipe':'rm -rf'},[],reason='bad')
        self.install('tool:inspect',p)
        preview=self.call('tool-preview',name='tool:inspect',workspace=self.temp.name)
        with self.assertRaises(ValueError):self.call('tool-run',name='tool:inspect',workspace=self.temp.name,packet_hash=preview['packet_hash'])
        result=self.call('tool-run',name='tool:inspect',workspace=self.temp.name,packet_hash=preview['packet_hash'],confirmed=True)
        self.assertEqual(result['tool_result']['status'],'completed')

    def test_tool_actual_tests_and_zero_tests_are_not_pass(self):
        self.install('tool:tests',{'kind':'tool','description':'Run project tests','recipe':'unittest','timeout':10})
        root=Path(self.temp.name)/'workspace';root.mkdir()
        def run():
            p=self.call('tool-preview',name='tool:tests',workspace=str(root))
            return self.call('tool-run',name='tool:tests',workspace=str(root),packet_hash=p['packet_hash'],confirmed=True)['tool_result']
        with patch.dict(os.environ,{'EH_ALLOW_WRITE':'1'}):
            self.assertEqual(run()['status'],'failed')
            (root/'test_real.py').write_text('import unittest\nclass Tests(unittest.TestCase):\n def test_ok(self): self.assertEqual(2+2,4)\n')
            result=run();self.assertEqual(result['status'],'passed');self.assertEqual(result['test_count'],1)

    def test_model_component_is_pending_evidence_bound_and_duplicates_blocked(self):
        self.c.seed('context')
        ref=self.store.event('alice','project','user_feedback',{'note':'Too many duplicate memories'})
        prepared=prepare(self.store,'alice','project','context')
        response={'payload':{'kind':'context','max_memories':3,'max_chars':1000,'deduplicate':True},
                  'reason':'Candidate to test, no demonstrated gain','evidence_ids':[ref]}
        candidate=save(self.store,'alice','project',prepared,response,'fixture','fixture')
        self.assertNotEqual(self.c.active('context')['id'],candidate['id'])
        with self.assertRaises(ValueError):save(self.store,'alice','project',prepared,response,'fixture','fixture')
        with self.assertRaises(ValueError):save(self.store,'alice','project',prepared,response|{'evidence_ids':[999999]},'fixture','fixture')

    def test_user_profile_and_component_approval_gates(self):
        template=self.call('component-template',name='profile',user_scope=True)['component_template']
        template['payload']['preferences']=['Chinese replies'];template['reason']='Explicit preference'
        candidate=self.call('component-stage',draft=template,user_scope=True)['component']
        gate=self.call('component-evaluate',id=candidate['id'],user_scope=True)['component_evaluation']
        with self.assertRaises(ValueError):self.call('component-activate',id=candidate['id'],evaluation_id=gate['evaluation_id'],user_scope=True)
        self.call('component-activate',id=candidate['id'],evaluation_id=gate['evaluation_id'],user_scope=True,confirmed=True)
        self.assertIn('Chinese replies',assemble(self.store,'alice','project','fix',[])['instructions'])

    def test_memory_rollback_restores_snapshot_as_new_revision(self):
        mid=Memory(self.store).add('alice','project','old','fixture',verified=True)
        r=Revisions(self.store,'alice','project');c=r.propose(mid)
        c=r.edit(c['id'],1,'revise','new','explicit correction');r.decide(c['id'],c['revision'],True)
        result=r.rollback(c['id'])
        self.assertEqual(result['rollback_revision'],3)
        self.assertEqual(Memory(self.store).get('alice','project',mid)['content'],'old')
        with self.assertRaises(ValueError):r.rollback(c['id'])

    def test_center_and_experiment_priority_are_scoped(self):
        self.call('strategy-feedback',check='expiry',note='old command')
        result=self.call('experiments-status')['experiments']
        self.assertEqual(result['next_checks'],['expiry'])
        center=self.call('product-status')['product']
        self.assertEqual({c['name'] for c in center['active']},{'reviewer','context'})

    def test_completed_task_auto_collects_fact_and_drafts_without_activation(self):
        task=self.call('begin',prompt='fix tests',workspace=self.temp.name)['task_id']
        self.call('event',task_id=task,event={'phase':'end','toolName':'powershell','output':'Ran 0 tests','isError':False})
        result=self.call('finish',task_id=task,state='completed',text='No claim of successful tests.')
        observation=result['strategy_observation']
        self.assertTrue(observation['feedback_ids']);self.assertTrue(observation['pending_candidate'])
        c=self.c.get(observation['pending_candidate'])
        self.assertEqual(c['payload']['checks'],['test_count'])
        self.assertNotEqual(self.c.active('reviewer')['id'],c['id'])
        self.assertEqual(Strategy(self.store,'alice','project').observe_task(task)['feedback_ids'],[])
