import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evolving_harness.cross_task import CrossTaskLab, reference
from evolving_harness.native import dispatch
from evolving_harness.store import Store

FIXED_TRAIN='def discounted_total(price, percent):\n    return price * (1 - percent / 100)\n'
FIXED_PAGE='def paginate(items, page, size):\n    return items[(page - 1) * size:page * size]\n'
FIXED_OTHER='def slug(text):\n    return text.strip().lower().replace(" ", "-")\n'


class ScriptedAgent:
    """Offline actor, not an autonomous LLM; reference tests really execute."""
    prompts=[]
    def __init__(self,command,cwd,timeout,on_event):
        self.workspace=Path(cwd);self.record=on_event
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def request(self,kind,**fields):
        return {'model':{'provider':'fixture','id':'fixture'}} if kind=='get_state' else {}
    async def prompt(self,prompt):
        self.prompts.append((self.workspace.name,prompt))
        source=FIXED_TRAIN if self.workspace.name=='train' else FIXED_OTHER if self.workspace.name=='other' else FIXED_PAGE
        (self.workspace/'rules.py').write_text(source)
        self.record({'type':'tool_execution_start','toolName':'powershell','toolCallId':'fixture',
                     'args':{'command':'python checks/run_tests.py'}})
        self.record({'type':'tool_execution_end','toolName':'powershell','toolCallId':'fixture','isError':False,
                     'result':{'content':[{'type':'text','text':'Ran 2 tests\nOK\n'}]}})
        return {'text':'Scripted fixture repaired code; not a real model run.'}


class CrossTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'state.db')
        dispatch(self.store,'alice','parent',{'operation':'status'})
        self.lab=CrossTaskLab(self.store,'alice','parent')
        self.lab.setup();ScriptedAgent.prompts=[]

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def run_stage(self,stage):
        preview=self.lab.preview(stage)
        with patch('evolving_harness.cross_task.pi_command',return_value=['fixture']), \
             patch('evolving_harness.cross_task.PiRpc',ScriptedAgent):
            return asyncio.run(self.lab.run(preview,'unused','fixture','fixture'))

    def train_and_accept(self):
        self.run_stage('train')
        result=self.lab.review();c=result['candidate']
        self.lab.accept(c['id'],c['revision'])

    def test_setup_reproduces_zero_discovery_and_real_failure_without_overwriting(self):
        state=self.lab.current()
        self.assertEqual(state['root_discovery']['test_count'],0)
        self.assertEqual(state['root_discovery']['status'],'no_tests')
        self.assertEqual(state['initial_reference']['test_count'],2)
        self.assertEqual(state['initial_reference']['status'],'failed')
        old=Path(state['root'])
        self.lab.setup()
        self.assertNotEqual(str(old),self.lab.current()['root'])
        self.assertTrue((old/'train/rules.py').is_file())

    def test_training_candidate_is_cited_and_needs_acceptance(self):
        with self.assertRaises(ValueError): self.lab.review()
        self.run_stage('train')
        result=self.lab.review();c=result['candidate']
        self.assertEqual(c['status'],'pending')
        self.assertEqual(c['backend'],'lab-reference-extraction')
        self.assertEqual(len(c['evidence_ids']),2)
        with self.assertRaises(ValueError): self.lab.preview('memory')
        self.lab.accept(c['id'],c['revision'])
        self.assertIsNotNone(self.lab.preview('memory')['lab_preview']['memory'])

    def test_cross_task_pair_keeps_baseline_clean_and_real_reference_tests_pass(self):
        self.train_and_accept()
        first=self.lab.preview('baseline')['lab_preview']
        second=self.lab.preview('memory')['lab_preview']
        self.assertEqual(first['files'],second['files'])
        self.assertEqual(first['initial_fingerprint'],second['initial_fingerprint'])
        self.assertIsNone(first['memory'])
        self.run_stage('baseline');result=self.run_stage('memory')
        self.assertEqual(result['comparison']['outcome'],'tie_on_this_pair')
        self.assertTrue(result['comparison']['baseline_passed'])
        self.assertTrue(result['comparison']['memory_passed'])
        self.assertEqual(result['isolation']['status'],'passed')
        for stage in ('baseline','memory'):
            self.assertEqual(result['lab']['stages'][stage]['verification']['test_count'],2)

    def test_other_project_has_different_entry_and_no_injected_lab_memory(self):
        self.train_and_accept()
        p=self.lab.preview('other')['lab_preview']
        self.assertIsNone(p['memory'])
        self.assertNotIn('checks/run_tests.py',p['files'])
        result=self.run_stage('other')
        self.assertEqual(result['lab']['stages']['other']['verification']['status'],'passed')
        context=self.store.events('alice',result['lab']['other_project'],'context_assembled')[-1]
        self.assertEqual(context['data']['memory_ids'],[])

    def test_test_tampering_is_rejected(self):
        state=self.lab.current();workspace=Path(state['stages']['train']['workspace'])
        (workspace/'checks/case_rules.py').write_text('import unittest\n')
        self.assertEqual(reference(workspace,'train')['status'],'protected_files_changed')

    def test_scope_and_approval_and_stale_preview_are_checked(self):
        with self.assertRaises(ValueError): CrossTaskLab(self.store,'bob','parent').current()
        p=self.lab.preview('train')
        with patch.dict(os.environ,{'EH_ALLOW_WRITE':'1'}):
            for values in [{},{'confirmed':True,'provider':'fixture','model':'fixture','packet_hash':'stale'}]:
                with self.assertRaises(ValueError):
                    dispatch(self.store,'alice','parent',{'operation':'lab-run','stage':'train',**values})
        (Path(p['lab_preview']['workspace'])/'rules.py').write_text('# manually changed')
        self.assertNotEqual(p['packet_hash'],self.lab.preview('train')['packet_hash'])

    def test_attempted_stage_cannot_be_replayed_to_pick_a_better_result(self):
        self.run_stage('train')
        with self.assertRaises(ValueError): self.lab.preview('train')

    def test_edited_candidate_uses_revision_gate(self):
        self.run_stage('train');c=self.lab.review()['candidate']
        changed=self.lab.edit('项目测试请运行 python checks/run_tests.py，并检查测试数量。',c['revision'])['candidate']
        with self.assertRaises(ValueError):self.lab.accept(c['id'],c['revision'])
        self.lab.accept(c['id'],changed['revision'])
        self.assertEqual(self.lab.status()['isolation']['status'],'passed')

    def test_nonidentical_holdout_or_failed_model_is_inconclusive(self):
        self.train_and_accept();self.run_stage('baseline');self.run_stage('memory')
        state=self.lab.current()
        state['stages']['memory']['initial_fingerprint']='different-copy'
        self.lab.save(state)
        self.assertEqual(self.lab.status()['comparison']['outcome'],'inconclusive')
        state['stages']['memory']['status']='failed'
        self.lab.save(state)
        self.assertEqual(self.lab.status()['comparison']['outcome'],'inconclusive')
