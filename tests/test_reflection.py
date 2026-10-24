import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from evolving_harness.native import dispatch
from evolving_harness.reflection import Reflection
from evolving_harness.store import Store


class ReflectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)
        self.store=Store(self.path/'state.db')
        self.task=self.call('begin',prompt='check example')['task_id']
        self.call('finish',task_id=self.task,state='completed',text='model claim: all passed')
        self.note=self.call('note',task_id=self.task,note='Shell unavailable; stop repeated attempts')['evidence_id']
        self.review=Reflection(self.store,'alice','repo')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def call(self,operation,**values):
        return dispatch(self.store,'alice','repo',{'operation':operation,**values})

    def proposal(self,**updates):
        return {'title':'Environment handling','content':'Check shell availability before retrying',
                'kind':'project_memory','basis':'user_report','evidence_ids':[self.note]} | updates

    def test_local_extraction_is_pending_idempotent_and_not_model_learning(self):
        first=self.review.local(self.task)
        second=self.review.local(self.task)
        self.assertEqual(len(first),1)
        self.assertEqual(first[0]['id'],second[0]['id'])
        self.assertEqual(first[0]['status'],'pending')
        self.assertEqual(first[0]['backend'],'local-feedback-extraction')
        self.assertEqual(first[0]['basis'],'user_report')
        self.assertEqual(self.call('memory-list')['memories'],[])

    def test_accept_confirmed_memory_is_used_in_later_task(self):
        candidate=self.review.local()[0]
        accepted=self.review.decide(candidate['id'],True,1)
        self.assertEqual(accepted['status'],'accepted_memory')
        next_task=self.call('begin',prompt='next repair')
        self.assertEqual(next_task['memories'][0]['id'],accepted['memory_id'])
        with self.assertRaises(ValueError):
            self.review.decide(candidate['id'],True,1)
        self.assertEqual(len(self.call('memory-list')['memories']),1)

    def test_edit_revision_reject_and_scope(self):
        candidate=self.review.local()[0]
        revised=self.review.edit(candidate['id'],'Report missing tools instead of endless retries',1)
        self.assertEqual(revised['revision'],2)
        with self.assertRaises(ValueError):
            self.review.decide(candidate['id'],True,1)
        with self.assertRaises(ValueError):
            Reflection(self.store,'bob','repo').get(candidate['id'])
        self.review.decide(candidate['id'],False,2)
        self.assertEqual(self.call('memory-list')['memories'],[])
        with self.assertRaises(ValueError):
            self.review.edit(candidate['id'],'cannot edit decided',2)

    def test_invalid_citations_and_basis_do_not_partially_persist(self):
        prepared=self.review.prepare()
        for bad in [self.proposal(evidence_ids=[999999]),self.proposal(basis='observation'),
                    self.proposal(kind='workflow',basis='user_report')]:
            with self.assertRaises(ValueError):
                self.review.save(prepared,[self.proposal(),bad],'fixture')
        self.assertEqual(self.review.list(),[])

    def test_workflow_review_cannot_promote_coding_skill_or_memory(self):
        original=self.store.current('alice','repo','coding')['id']
        candidate=self.review.save(self.review.prepare(),[self.proposal(kind='workflow',basis='hypothesis')],'fixture')[0]
        result=self.review.decide(candidate['id'],True,1)
        self.assertEqual(result['status'],'reviewed_workflow')
        self.assertIsNone(result['memory_id'])
        self.assertEqual(self.store.current('alice','repo','coding')['id'],original)
        self.assertEqual(self.call('memory-list')['memories'],[])

    def test_model_requires_confirmation_and_matching_preview(self):
        preview=self.call('review-preview')
        with self.assertRaises(ValueError):
            self.call('review-model')
        self.call('note',note='new evidence')
        with self.assertRaises(ValueError):
            self.call('review-model',confirmed=True,provider='fixture',model='fixture',
                      packet_hash=preview['packet_hash'],pi_root='unused')

    def test_model_protocol_uses_no_tools_and_strict_json(self):
        rpc=AsyncMock()
        rpc.prompt.return_value={'text':json.dumps({'candidates':[self.proposal()]})}
        with patch('evolving_harness.reflection.pi_command',return_value=['node','fixture']) as command, \
             patch('evolving_harness.reflection.PiRpc') as cls:
            cls.return_value.__aenter__=AsyncMock(return_value=rpc)
            cls.return_value.__aexit__=AsyncMock(return_value=None)
            result=asyncio.run(self.review.model(self.review.prepare(),'unused','fixture','fixture'))
        self.assertFalse(command.call_args.kwargs['tools'])
        self.assertEqual(result[0]['backend'],'pi-rpc-reflection')
        prompt=rpc.prompt.call_args.args[0]
        self.assertIn('User feedback is a user report',prompt)
        self.assertNotIn('model claim: all passed',prompt)

    def test_no_evidence_gives_no_local_candidates(self):
        new=self.call('begin',prompt='another task')['task_id']
        self.call('finish',task_id=new,state='completed')
        self.assertEqual(self.review.local(new),[])

    def test_other_task_evidence_cannot_be_smuggled_into_packet(self):
        new=self.call('begin',prompt='other task')['task_id']
        self.call('finish',task_id=new,state='completed')
        foreign=self.call('note',task_id=new,note='different task')['evidence_id']
        prepared=self.review.prepare(self.task)
        prepared['packet']['evidence'].append({'id':foreign,'kind':'user_feedback','data':{}})
        with self.assertRaises(ValueError):
            self.review.save(prepared,[self.proposal(evidence_ids=[foreign])],'fixture')

    def test_invalid_model_json_cannot_create_candidates(self):
        rpc=AsyncMock()
        rpc.prompt.return_value={'text':'```json\n{}\n```'}
        with patch('evolving_harness.reflection.pi_command',return_value=['fixture']), \
             patch('evolving_harness.reflection.PiRpc') as cls:
            cls.return_value.__aenter__=AsyncMock(return_value=rpc)
            cls.return_value.__aexit__=AsyncMock(return_value=None)
            with self.assertRaises(ValueError):
                asyncio.run(self.review.model(self.review.prepare(),'unused','fixture','fixture'))
        self.assertEqual(self.review.list(),[])
