import asyncio
import json
import unittest
from unittest.mock import patch

import test_product as product_tests
from evolving_harness.component_model import prepare,generate,parse_response,ProposalFailure


class ComponentResponseTests(unittest.TestCase):
    def setUp(self):
        product_tests.ProductTests.setUp(self)
        self.c.seed('reviewer')
        self.ref=self.store.event('alice','project','user_feedback',{'note':'Test command was stale'})
        self.prepared=prepare(self.store,'alice','project','reviewer')

    def tearDown(self):product_tests.ProductTests.tearDown(self)

    def payload(self):
        return {'payload':{'kind':'reviewer','checks':['expiry'],'priority':['scope','expiry','duplicate','test_count']},
                'reason':'Use observed configuration conflict; gains unverified.','evidence_ids':[self.ref]}

    def run_response(self,text):
        class Rpc:
            def __init__(self,*args):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def request(self,kind,**kw):return {'model':{'provider':'fixture','id':'fixture'}}
            async def prompt(self,prompt):
                return {'text':text,'message':{'stopReason':'stop','content':[{'type':'thinking','thinking':'DO_NOT_STORE_THIS'}]}}
        with patch('evolving_harness.component_model.pi_command',return_value=['fixture']),patch('evolving_harness.component_model.PiRpc',Rpc):
            return asyncio.run(generate(self.store,'alice','project',self.prepared,'unused','fixture','fixture'))

    def test_fenced_json_is_normalized_but_candidate_not_activated(self):
        c=self.run_response('```json\n'+json.dumps(self.payload())+'\n```')
        self.assertNotEqual(self.c.active('reviewer')['id'],c['id'])

    def test_prose_duplicate_keys_constants_and_multiple_objects_rejected(self):
        for text in ['Here is JSON: {"payload":{}}','{"a":1,"a":2}','{"a":NaN}','{} {}','[]']:
            with self.assertRaises(ValueError):parse_response(text)

    def test_invalid_json_preserved_without_hidden_reasoning_or_candidate(self):
        with self.assertRaises(ProposalFailure) as failure:self.run_response('not JSON')
        self.assertEqual(failure.exception.diagnostic['status'],'invalid_json')
        event=self.store.events('alice','project','component_proposal_outcome')[-1]
        self.assertEqual(event['data']['response_text'],'not JSON')
        self.assertNotIn('DO_NOT_STORE_THIS',json.dumps(event))
        self.assertEqual(len(self.c.history()),1)

    def test_empty_response_is_distinct_from_schema_failure(self):
        for text,status in [('', 'empty_response'),('{}','invalid_candidate')]:
            with self.assertRaises(ProposalFailure) as failure:self.run_response(text)
            self.assertEqual(failure.exception.diagnostic['status'],status)

    def test_same_payload_is_reported_as_unchanged(self):
        response=self.payload();response['payload']=self.c.active('reviewer')['payload']
        with self.assertRaises(ProposalFailure) as failure:self.run_response(json.dumps(response))
        self.assertEqual(failure.exception.diagnostic['status'],'unchanged')

    def test_foreign_evidence_stays_rejected_even_when_wrapped_in_json_fence(self):
        response=self.payload();response['evidence_ids']=[999999]
        with self.assertRaises(ProposalFailure) as failure:self.run_response('```json\n'+json.dumps(response)+'\n```')
        self.assertEqual(failure.exception.diagnostic['status'],'invalid_candidate')
        self.assertEqual(len(self.c.history()),1)
