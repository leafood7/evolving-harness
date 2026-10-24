import asyncio
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from evolving_harness.components import Components
from evolving_harness.strategy import Strategy
from evolving_harness.store import Store
from evolving_harness.review_benchmark import prepare,run,score,CASES
from evolving_harness.native import dispatch


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.s=Store(Path(self.temp.name)/'state.db')
        strategy=Strategy(self.s,'fixture','p');strategy.feedback('scope','fixture')
        self.c=strategy.propose();self.p=prepare(self.s,'fixture','p',self.c['id'])

    def tearDown(self):self.s.close();self.temp.cleanup()

    def test_labels_not_in_preview_and_confirmation_is_required(self):
        self.assertNotIn('expected',json.dumps(self.p))
        with self.assertRaises(ValueError):dispatch(self.s,'fixture','p',{'operation':'product-benchmark-run','id':self.c['id']})

    def test_bad_output_and_fake_zero_test_success_fail(self):
        self.assertFalse(score(CASES[0],'not JSON')['valid'])
        p={'action':'keep','content':CASES[2]['memory'],'reason':'success','evidence_ids':[1]}
        self.assertFalse(score(CASES[2],json.dumps(p))['correct'])

    def test_mocked_independent_requests_keep_tie_without_promotion(self):
        calls=[]
        class Rpc:
            def __init__(self,*args):calls.append(args)
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def request(self,kind,**kw):return {'model':{'provider':'fixture','id':'fixture'}}
            async def prompt(self,prompt):
                case=json.loads(prompt.split('Evidence JSON:\n')[1])
                response={'action':'keep','content':case['memory'],'reason':'fixture only','evidence_ids':[1,2]}
                if case['name']=='stale_entry':response.update(action='revise',content='Run python checks/run_tests.py and count tests.')
                if case['name']=='zero_tests':response.update(action='revise',content='Check actual test count; 0 tests is unverified.')
                return {'text':json.dumps(response)}
        with patch('evolving_harness.review_benchmark.PiRpc',Rpc),patch('evolving_harness.review_benchmark.pi_command',return_value=['fixture']):
            report=asyncio.run(run(self.s,'fixture','p',self.p,'unused','fixture','fixture'))['benchmark_result']
        self.assertEqual(len(calls),6);self.assertEqual(report['outcome'],'tie_on_this_suite')
        self.assertEqual(report['correct'],{'baseline':3,'candidate':3})
        self.assertNotEqual(Components(self.s,'fixture','p').active('reviewer')['id'],self.c['id'])
