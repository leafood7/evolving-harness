import unittest

import test_product as product_tests
from evolving_harness.improvement_batch import ImprovementBatch,track
from evolving_harness.memory import Memory
from evolving_harness.revision_model import prepare,save


class BatchTests(unittest.TestCase):
    def setUp(self):
        product_tests.ProductTests.setUp(self)
        self.batch=ImprovementBatch(self.store,'alice','project')

    def tearDown(self):product_tests.ProductTests.tearDown(self)

    def call(self,operation,**kw):return product_tests.ProductTests.call(self,operation,**kw)

    def ready(self):
        task=self.call('begin',prompt='run tests',workspace=self.temp.name)['task_id']
        self.call('event',task_id=task,event={'phase':'end','toolName':'powershell','output':'Ran 0 tests','isError':False})
        self.call('finish',task_id=task,state='completed',text='Zero tests, not verified.')
        return self.batch.plan()['improvement_batch']

    def test_no_evidence_keeps_current_version_without_candidate(self):
        result=self.batch.plan()['improvement_batch']
        self.assertEqual(result['status'],'no_change');self.assertIsNone(result['candidate'])
        self.assertEqual(len(self.c.history()),1)

    def test_evaluation_is_automatic_activation_still_requires_confirmation(self):
        result=self.ready()
        self.assertEqual(result['status'],'ready');self.assertEqual(result['evaluation']['decision'],'accept')
        self.assertNotEqual(self.c.active('reviewer')['id'],result['candidate'])
        with self.assertRaises(ValueError):self.call('product-improve-activate',id=result['id'])
        activated=self.call('product-improve-activate',id=result['id'],confirmed=True)['improvement_batch']
        self.assertEqual(activated['status'],'awaiting_followup')
        with self.assertRaises(ValueError):self.call('product-improve-activate',id=result['id'],confirmed=True)

    def test_followup_distinguishes_task_configuration_from_model_review(self):
        result=self.ready();self.batch.activate(result['id'])
        mid=Memory(self.store).add('alice','project','Use current project command','fixture',verified=True)
        self.call('begin',prompt='inspect memory',workspace=self.temp.name)
        follow=self.batch.status()['improvement_batch']['followup']
        self.assertEqual([e['kind'] for e in follow],['task_context'])
        p=prepare(self.store,'alice','project',mid)
        candidate=save(self.store,'alice','project',p,{'action':'keep','content':p['revision_preview']['memory']['content'],
            'reason':'Offline fixture only','evidence_ids':[p['revision_preview']['evidence'][0]['id']]},'fixture','fixture')
        follow=self.batch.status()['improvement_batch']['followup']
        self.assertEqual([e['kind'] for e in follow],['task_context','model_review'])
        self.assertEqual(candidate['generator_id'],result['candidate'])

    def test_other_scope_cannot_activate_batch_and_tracking_survives_later_noop(self):
        result=self.ready()
        with self.assertRaises(ValueError):ImprovementBatch(self.store,'bob','project').activate(result['id'])
        self.batch.activate(result['id']);self.batch.plan()
        track(self.store,'alice','project',result['candidate'],'model_review','fixture-reference')
        events=self.store.events('alice','project','improvement_followup')
        self.assertEqual(events[-1]['data']['batch'],result['id'])
        track(self.store,'alice','project',result['candidate'],'model_review','fixture-reference')
        self.assertEqual(len(self.store.events('alice','project','improvement_followup')),1)
