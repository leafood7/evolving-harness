import asyncio
import json
import unittest
from unittest.mock import patch

import test_memory_revision as revision_tests
from evolving_harness.native import dispatch
from evolving_harness.revision_model import prepare, save, generate


class ModelRevisionTests(unittest.TestCase):
    def setUp(self):
        revision_tests.RevisionTests.setUp(self)
        task=self.store.task('alice','project','test entry')
        self.store.event('alice','project','context_assembled',{'memory_ids':[self.mid]},task)
        self.ref=self.store.event('alice','project','user_feedback',{'note':'Use new test entry instead of old entry.'},task)
        self.prepared=prepare(self.store,'alice','project',self.mid)

    def tearDown(self):
        revision_tests.RevisionTests.tearDown(self)

    def response(self):
        return {'action':'revise','content':'new test entry','reason':'User correction; benefit unverified.', 'evidence_ids':[self.ref]}

    def test_model_proposal_stays_pending_and_does_not_edit_memory(self):
        c=save(self.store,'alice','project',self.prepared,self.response(),'fixture','fixture')
        self.assertEqual(c['status'],'pending')
        self.assertEqual(c['generator'],'pi-rpc-memory-review')
        self.assertEqual(self.memory.get('alice','project',self.mid)['revision'],1)

    def test_invalid_schema_refs_or_fake_keep_do_not_persist(self):
        invalid=[self.response()|{'evidence_ids':[999999]},self.response()|{'extra':True},
                 self.response()|{'action':'keep'},self.response()|{'content':''},
                 self.response()|{'evidence_ids':[True]}]
        for response in invalid:
            with self.assertRaises(ValueError): save(self.store,'alice','project',self.prepared,response,'fixture','fixture')
        self.assertEqual(self.revisions.list(),[])

    def test_changed_evidence_or_memory_rejects_generated_candidate(self):
        self.store.event('alice','project','user_feedback',{'note':'new observation'},
            self.store.events('alice','project','context_assembled')[0]['task_id'])
        with self.assertRaises(ValueError): save(self.store,'alice','project',self.prepared,self.response(),'fixture','fixture')

    def test_confirmation_and_preview_hash_gate_runs_before_model(self):
        request={'operation':'memory-revision-model','memory_id':self.mid,'provider':'fixture','model':'fixture','pi_root':'unused'}
        with self.assertRaises(ValueError): dispatch(self.store,'alice','project',request)
        with self.assertRaises(ValueError): dispatch(self.store,'alice','project',request|{'confirmed':True,'packet_hash':'stale'})

    def test_mock_rpc_is_no_tool_and_returns_validated_pending_candidate(self):
        response=self.response()
        class Rpc:
            def __init__(self,*args): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def request(self,kind,**kw): return {'model':{'provider':'fixture','id':'fixture'}}
            async def prompt(self,prompt): return {'text':json.dumps(response)}
        with patch('evolving_harness.revision_model.pi_command',return_value=['fixture']) as command, \
             patch('evolving_harness.revision_model.PiRpc',Rpc):
            c=asyncio.run(generate(self.store,'alice','project',self.prepared,'unused','fixture','fixture'))
        self.assertFalse(command.call_args.kwargs['tools'])
        self.assertEqual(c['status'],'pending')
