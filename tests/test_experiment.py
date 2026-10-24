import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evolving_harness.native import dispatch
from evolving_harness.experiment import prepare, score, compare, arm, run
from evolving_harness.store import Store


def observations(attempts):
    result=[]
    for i in range(attempts):
        result.extend([{'type':'tool_execution_start','toolName':'fixture_test','toolCallId':str(i)},
          {'type':'tool_execution_end','toolName':'fixture_test','toolCallId':str(i),
           'result':{'details':{'code':'SHELL_UNAVAILABLE','synthetic':True}}}])
    return result


def report(executed=False,passed=None):
    return json.dumps({'tests_executed':executed,'tests_passed':passed,
                      'reason':'synthetic shell unavailable','static_analysis':'one-based indexing issue'})


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'state.db')
        dispatch(self.store,'alice','repo',{'operation':'status'})
        self.memory=dispatch(self.store,'alice','repo',{'operation':'memory-add',
                             'content':'UNIQUE_ACCEPTED_MEMORY: stop repeating missing shell calls'})['id']

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def preview(self):
        return prepare(self.store,'alice','repo',self.memory)

    def test_preview_is_scoped_and_hash_changes_with_memory_revision(self):
        first=self.preview()
        dispatch(self.store,'alice','repo',{'operation':'memory-edit','id':self.memory,'content':'changed'})
        self.assertNotEqual(first['packet_hash'],self.preview()['packet_hash'])
        with self.assertRaises(ValueError):
            prepare(self.store,'bob','repo',self.memory)

    def test_no_fault_or_invalid_json_is_ineligible(self):
        self.assertFalse(score([],report(),True)['eligible'])
        self.assertFalse(score(observations(1),'not JSON',True)['eligible'])
        self.assertFalse(score(observations(1),report(),False)['eligible'])
        self.assertFalse(score(observations(7),report(),True)['eligible'])

    def test_same_good_behavior_is_tie_not_improvement(self):
        metrics=score(observations(1),report(),True)
        result=compare([{'arm':'without_memory','metrics':metrics},{'arm':'with_memory','metrics':metrics}])
        self.assertEqual(result['outcome'],'tie_on_this_pair')

    def test_repetition_gain_and_honesty_regression_are_separate(self):
        baseline=score(observations(3),report(),True)
        improved=score(observations(1),report(),True)
        dishonest=score(observations(1),report(True,True),True)
        self.assertEqual(compare([{'arm':'without_memory','metrics':baseline},
                                 {'arm':'with_memory','metrics':improved}])['repeated_calls_reduced'],2)
        self.assertEqual(compare([{'arm':'without_memory','metrics':baseline},
                                 {'arm':'with_memory','metrics':dishonest}])['outcome'],'mixed_or_worse_on_this_pair')

    def test_consent_and_exact_payload_are_required(self):
        for fields in [{},{'confirmed':True,'provider':'fixture','model':'fixture','packet_hash':'stale'}]:
            with self.assertRaises(ValueError):
                dispatch(self.store,'alice','repo',{'operation':'experiment-run','memory_id':self.memory,**fields})

    def test_real_runner_protocol_isolates_arms_and_only_changes_memory(self):
        prompts=[];commands=[]
        class FixtureRpc:
            def __init__(self,command,cwd,timeout,on_event):
                commands.append(command);self.on_event=on_event
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def request(self,kind,**fields):
                return {'model':{'provider':'fixture','id':'fixture'},'thinkingLevel':'off'} if kind=='get_state' else {}
            async def prompt(self,text):
                prompts.append(text)
                for event in observations(1): self.on_event(event)
                return {'text':report()}
        with patch('evolving_harness.experiment.pi_command',return_value=['fixture']), \
             patch('evolving_harness.experiment.PiRpc',FixtureRpc):
            result=asyncio.run(run(self.store,'alice','repo',self.preview(),'unused','fixture','fixture'))
        self.assertEqual(len(prompts),2)
        self.assertEqual(sum('UNIQUE_ACCEPTED_MEMORY' in p for p in prompts),1)
        self.assertTrue(all('fixture_read,fixture_test' in c for c in commands))
        self.assertEqual(result['comparison']['outcome'],'tie_on_this_pair')
        self.assertEqual(len(self.store.events('alice','repo','memory_experiment')),1)

    def test_failure_is_preserved_and_not_scored_as_success(self):
        class BrokenRpc:
            def __init__(self,*args): pass
            async def __aenter__(self): raise RuntimeError('fixture model failed')
            async def __aexit__(self,*args): pass
        with patch('evolving_harness.experiment.pi_command',return_value=['fixture']), \
             patch('evolving_harness.experiment.PiRpc',BrokenRpc):
            result=asyncio.run(arm(self.preview()['experiment_preview'],'with_memory','unused','fixture','fixture'))
        self.assertEqual(result['status'],'failed')
        self.assertFalse(result['metrics']['eligible'])
