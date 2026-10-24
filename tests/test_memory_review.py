import tempfile
import unittest
from pathlib import Path

from evolving_harness.store import Store
from evolving_harness.memory import Memory
from evolving_harness.native import dispatch


class MemoryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state.db')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def report(self, **kwargs):
        return dispatch(self.store, 'alice', 'parent', {'operation': 'memory-review', **kwargs})['memory_review']

    def test_empty_and_unobserved_memory_do_not_claim_benefit(self):
        self.assertEqual(self.report()['items'], [])
        mid = Memory(self.store).add('alice', 'parent', 'test command', 'user')
        item = self.report()['items'][0]
        self.assertEqual(item['assessment'], 'insufficient_evidence')
        self.assertEqual(item['usage'], [])
        self.assertEqual(Memory(self.store).get('alice', 'parent', mid)['revision'], 1)

    def test_other_owner_and_unlinked_project_are_inaccessible(self):
        mid = Memory(self.store).add('bob', 'parent', 'private', 'user')
        hidden = Memory(self.store).add('alice', 'unlinked', 'private', 'user')
        for key in (mid, hidden):
            with self.assertRaises(ValueError): self.report(memory_id=key)

    def pair(self):
        mid = Memory(self.store).add('alice', 'lab:primary', 'run tests', 'fixture')
        stages = {}
        for name in ('baseline', 'memory'):
            task = self.store.task('alice', 'lab:primary', name)
            self.store.event('alice', 'lab:primary', 'context_assembled',
                {'memory_ids': [mid] if name == 'memory' else [], 'memory_context':
                 [{'id': mid, 'content': 'run tests', 'revision': 1}] if name == 'memory' else []}, task)
            self.store.event('alice', 'lab:primary', 'verification', {'status':'passed','test_count':2}, task)
            self.store.finish_task(task, 'completed', 'fixture')
            stages[name] = {'task_id':task,'status':'completed','verification':{'status':'passed'},
                            'model':{'id':'fixture'},'initial_fingerprint':'same','zero_test_calls':0}
        state = {'primary_project':'lab:primary','memory_id':mid,'stages':stages}
        self.store.event('alice', 'parent', 'cross_task_lab_state', state)
        return state, mid

    def test_linked_pair_has_citations_and_tie_does_not_mean_gain(self):
        state, mid = self.pair()
        item = self.report(memory_id=mid)['items'][0]
        self.assertEqual(item['source'], 'latest_linked_lab')
        self.assertEqual(item['assessment'], 'no_demonstrated_gain')
        self.assertEqual(len(item['usage']), 1)
        self.assertFalse(item['pair']['baseline']['injected'])
        self.assertTrue(item['pair']['memory']['evidence_ids'])

    def test_changed_model_or_memory_revision_invalidates_comparison(self):
        state, mid = self.pair()
        state['stages']['memory']['model'] = {'id':'different'}
        self.store.event('alice','parent','cross_task_lab_state',state)
        self.assertEqual(self.report()['items'][0]['assessment'], 'insufficient_evidence')
        state['stages']['memory']['model'] = {'id':'fixture'}
        self.store.event('alice','parent','cross_task_lab_state',state)
        Memory(self.store).edit('alice','lab:primary',mid,'new rule','user',True)
        self.assertEqual(self.report()['items'][0]['assessment'], 'insufficient_evidence')
