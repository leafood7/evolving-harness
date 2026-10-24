import tempfile
import unittest
from pathlib import Path

from evolving_harness.store import Store
from evolving_harness.memory import Memory
from evolving_harness.memory_revision import Revisions
from evolving_harness.native import dispatch


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'state.db')
        dispatch(self.store,'alice','project',{'operation':'status'})
        self.memory = Memory(self.store)
        self.mid = self.memory.add('alice','project','old test command','user',verified=True)
        self.revisions = Revisions(self.store,'alice','project')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_default_keep_is_idempotent_and_accept_does_not_invent_version(self):
        c = self.revisions.propose(self.mid)
        self.assertEqual(c['action'],'keep')
        self.assertEqual(self.revisions.propose(self.mid)['id'],c['id'])
        self.revisions.decide(c['id'],1,True)
        self.assertEqual(self.memory.get('alice','project',self.mid)['revision'],1)
        self.assertEqual(self.revisions.get(c['id'])['status'],'accepted')

    def test_edit_accept_preserves_history_and_future_retrieval_uses_revision(self):
        c = self.revisions.propose(self.mid)
        edited = self.revisions.edit(c['id'],1,'revise','new test command','explicit user correction')
        self.assertEqual(self.memory.get('alice','project',self.mid)['content'],'old test command')
        self.revisions.decide(c['id'],edited['revision'],True)
        m = self.memory.retrieve('alice','project','test command')[0]
        self.assertEqual((m['revision'],m['content']),(2,'new test command'))
        history = self.store.events('alice','project','memory_revision_candidate')
        self.assertEqual(len(history),3)
        self.assertEqual(history[0]['data']['before']['content'],'old test command')
        self.assertEqual(history[-1]['data']['result_memory_revision'],2)

    def test_reject_and_retire_are_distinct_and_retirement_preserves_record(self):
        c = self.revisions.propose(self.mid)
        self.revisions.decide(c['id'],1,False)
        self.assertEqual(self.memory.get('alice','project',self.mid)['revision'],1)
        c = self.revisions.propose(self.mid)
        c = self.revisions.edit(c['id'],1,'retire',reason='obsolete')
        self.revisions.decide(c['id'],c['revision'],True)
        self.assertEqual(self.memory.retrieve('alice','project','test'),[])
        self.assertEqual(self.memory.get('alice','project',self.mid)['status'],'inactive')

    def test_stale_candidate_and_stale_memory_and_repeated_decision_rejected(self):
        c = self.revisions.propose(self.mid)
        edited = self.revisions.edit(c['id'],1,'revise','new','user')
        with self.assertRaises(ValueError): self.revisions.decide(c['id'],1,True)
        self.memory.edit('alice','project',self.mid,'changed externally','user',True)
        with self.assertRaises(ValueError): self.revisions.decide(c['id'],edited['revision'],True)
        self.assertEqual(self.revisions.get(c['id'])['status'],'pending')
        self.revisions.decide(c['id'],edited['revision'],False)
        with self.assertRaises(ValueError): self.revisions.decide(c['id'],edited['revision'],False)

    def test_scope_and_bridge_confirmation_and_input_validation(self):
        c = self.revisions.propose(self.mid)
        with self.assertRaises(ValueError): Revisions(self.store,'bob','project').get(c['id'])
        with self.assertRaises(ValueError): Revisions(self.store,'alice','other').get(c['id'])
        with self.assertRaises(ValueError):
            dispatch(self.store,'alice','project',{'operation':'memory-revision-decide','id':c['id'],'revision':1,'accept':True})
        for action,content,reason in [('revise','','reason'),('retire',None,''),('unknown',None,'reason')]:
            with self.assertRaises(ValueError): self.revisions.edit(c['id'],1,action,content,reason)

    def test_deleted_memory_cannot_be_recreated_by_acceptance(self):
        c = self.revisions.propose(self.mid)
        self.memory.forget('alice','project',self.mid)
        with self.assertRaises(ValueError): self.revisions.decide(c['id'],1,True)
        self.assertEqual(self.memory.list('alice','project'),[])

    def test_linked_lab_candidate_only_changes_lab_memory(self):
        mid = self.memory.add('alice','lab:primary','lab command','fixture',verified=True)
        self.store.event('alice','project','cross_task_lab_state',
            {'primary_project':'lab:primary','memory_id':mid,'stages':{'baseline':{},'memory':{}}})
        c = self.revisions.propose(mid)
        c = self.revisions.edit(c['id'],1,'revise','new lab command','user')
        self.revisions.decide(c['id'],c['revision'],True)
        self.assertEqual(self.memory.get('alice','lab:primary',mid)['revision'],2)
        self.assertEqual(self.memory.get('alice','project',self.mid)['revision'],1)
