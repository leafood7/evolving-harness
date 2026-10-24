import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evolving_harness.store import Store
from evolving_harness.native import dispatch
from evolving_harness.components import Components
from evolving_harness.skill_compare import SkillCompare,reference,compare,source_files

PREFIX='''import json
from datetime import datetime
class Record:
 def __init__(self,t,l,m,n):self.t,self.l,self.m,self.n=t,l,m,n
 def as_dict(self):return {'time':self.t.isoformat(),'level':self.l,'message':self.m,'line':self.n}
'''
PARSERS='''def parse_line(text,n):
 t,l,m=text.split(' ',2)
 return Record(datetime.fromisoformat(t.replace('Z','+00:00')),l,m,n)
def parse_json_line(text,n):
 p=json.loads(text)
 return Record(datetime.fromisoformat(p['timestamp'].replace('Z','+00:00')),p['level'],p['message'],n)
'''
FILES={'AGENTS.md':'Run python checks/run_tests.py; preserve tests.',
 'checks/run_tests.py':"import sys,unittest\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]))\nr=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests'))\nraise SystemExit(0 if r.wasSuccessful() and r.testsRun else 1)\n",
 'log_inspector/__init__.py':'','log_inspector/__main__.py':'',
 'log_inspector/core.py':PREFIX+'\n# PARSERS\n'+PARSERS,
 'tests/test_logs.py':"import unittest\nfrom log_inspector.core import parse_line\nclass Test(unittest.TestCase):\n def test_message(self):self.assertEqual(parse_line('2026-10-24T01:00:00Z INFO ok',1).as_dict()['message'],'ok')\n def test_line(self):self.assertEqual(parse_line('2026-10-24T01:00:00Z INFO ok',2).as_dict()['line'],2)\n"}


class ScriptedRpc:
    prompts=[]
    def __init__(self,command,cwd,timeout,observe):self.root=Path(cwd);self.observe=observe
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def request(self,kind,**kw):return {'model':{'provider':'fixture','id':'fixture'}}
    async def prompt(self,prompt):
        self.prompts.append(prompt)
        (self.root/'log_inspector/parsers.py').write_text('from .core import Record\nimport json\nfrom datetime import datetime\n'+PARSERS)
        (self.root/'log_inspector/core.py').write_text(PREFIX+'\nfrom .parsers import parse_line,parse_json_line\n')
        command='python -I -X utf8 checks/run_tests.py' if 'Managed workflow:' in prompt else 'python checks/run_tests.py'
        self.observe({'type':'tool_execution_start','toolName':'powershell','toolCallId':'fixture','args':{'command':command}})
        self.observe({'type':'tool_execution_end','toolName':'powershell','toolCallId':'fixture','isError':False,
                      'result':{'content':[{'type':'text','text':'Ran 2 tests\nOK\n'}]}})
        return {'text':'Offline scripted actor, not model evidence.','message':{'usage':{'input':1,'output':1}}}


class SkillCompareTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'source';self.root.mkdir()
        for name,text in FILES.items():
            p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        self.s=Store(Path(self.temp.name)/'state.db');dispatch(self.s,'alice','p',{'operation':'status'})
        c=Components(self.s,'alice','p')
        skill=c.propose('skill:portable-test-entry',{'kind':'skill','description':'Check isolated imports','triggers':['导入'],
            'steps':['Run python -I -X utf8 checks/run_tests.py when imports change.']},[],reason='fixture')
        gate=c.evaluate(skill['id']);c.promote(skill['id'],gate['evaluation_id'])
        self.trial=SkillCompare(self.s,'alice','p');ScriptedRpc.prompts=[]

    def tearDown(self):self.s.close();self.temp.cleanup()

    def test_preview_pins_identical_copies_and_only_intended_skill(self):
        p=self.trial.setup(str(self.root));state=p['skill_comparison_preview']
        self.assertEqual(source_files(state['arms']['without_skill']['workspace']),source_files(state['arms']['with_skill']['workspace']))
        self.assertNotIn('python -I',state['task'])
        self.assertEqual(state['skill']['name'],'skill:portable-test-entry')

    def test_scripted_pair_real_tests_keep_tie_and_measure_coverage_separately(self):
        p=self.trial.setup(str(self.root))
        with patch('evolving_harness.skill_compare.PiRpc',ScriptedRpc),patch('evolving_harness.skill_compare.pi_command',return_value=['fixture']):
            report=asyncio.run(self.trial.run(p,'unused','fixture','fixture'))['skill_comparison_result']
        self.assertEqual(report['comparison']['outcome'],'tie_on_this_task')
        self.assertTrue(report['original_source_unchanged'])
        self.assertFalse(report['results']['without_skill']['metrics']['isolated_check_observed'])
        self.assertTrue(report['results']['with_skill']['metrics']['isolated_check_observed'])
        self.assertEqual(len(ScriptedRpc.prompts),2)
        with self.assertRaises(ValueError):asyncio.run(self.trial.run(p,'unused','fixture','fixture'))

    def test_protected_tests_changed_invalidates_evaluation(self):
        p=self.trial.setup(str(self.root));state=p['skill_comparison_preview'];root=Path(state['arms']['with_skill']['workspace'])
        (root/'tests/test_logs.py').write_text('')
        self.assertEqual(reference(root,state['files'])['status'],'protected_changed')

    def test_confirmation_scope_and_write_permissions_are_required(self):
        p=self.trial.setup(str(self.root))
        with self.assertRaises(ValueError):dispatch(self.s,'alice','p',{'operation':'product-skill-compare-run','packet_hash':p['packet_hash']})
        with self.assertRaises(ValueError):SkillCompare(self.s,'bob','p').current()
        with patch.dict(os.environ,{'EH_ALLOW_WRITE':'0'}):
            with self.assertRaises(ValueError):dispatch(self.s,'alice','p',{'operation':'product-skill-compare-run','confirmed':True,'provider':'fixture','model':'fixture','packet_hash':p['packet_hash']})

    def test_stale_copy_and_already_completed_task_refused(self):
        p=self.trial.setup(str(self.root));state=p['skill_comparison_preview']
        Path(state['arms']['with_skill']['workspace'],'README.md').write_text('changed after preview')
        with self.assertRaises(ValueError):asyncio.run(self.trial.run(p,'unused','fixture','fixture'))
        (self.root/'log_inspector/parsers.py').write_text('')
        with self.assertRaises(ValueError):self.trial.setup(str(self.root))

    def test_declared_invalid_or_failed_pair_does_not_claim_gain(self):
        self.assertEqual(compare({})['outcome'],'inconclusive')
        r={'status':'completed','reference':{'status':'protected_changed'},'metrics':{'isolated_check_observed':True}}
        self.assertEqual(compare({'without_skill':r,'with_skill':r})['outcome'],'inconclusive')
