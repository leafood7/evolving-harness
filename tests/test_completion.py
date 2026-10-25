import json
import tempfile
from pathlib import Path
import unittest

from evolving_harness.completion import build, export
from evolving_harness.components import Components
from evolving_harness.evidence import Evidence, snapshot
from evolving_harness.native import dispatch
from evolving_harness.store import Store


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        (self.workspace / 'app.py').write_text('value=1\n', encoding='utf-8')
        self.s = Store(self.root / 'state.db'); self.e = Evidence(self.s, 'alice', 'p')

    def tearDown(self): self.s.close(); self.temp.cleanup()

    def call(self, op, **kwargs): return dispatch(self.s, 'alice', 'p', {'operation':op, **kwargs})

    def begin(self, prompt='Implement <script>alert(1)</script>'):
        return self.call('begin', prompt=prompt, workspace=str(self.workspace), model={'provider':'fixture','id':'offline'})['task_id']

    def verified(self, task, **extra):
        snap = snapshot(str(self.workspace))
        self.e.record(task, 'verification', {'status':'passed','cwd':str(self.workspace),
            'test_count':2,'after_snapshot':snap,'protected_intact':True,'integrity_ok':True, **extra})

    def test_model_claim_and_missing_data_are_never_green(self):
        task = self.begin()
        response = self.call('finish', task_id=task, state='completed', text='All tests passed; cost zero')
        card = response['completion_card']
        self.assertEqual(card['decision'], 'needs_verification')
        self.assertIsNone(card['reported_cost']); self.assertIsNone(card['elapsed_seconds'])
        self.assertIsNone(card['verification']['protected_intact'])
        self.assertIn('未验证', Path(response['card_html']).read_text(encoding='utf-8'))

    def test_verified_card_has_current_fingerprint_evidence_and_escaped_html(self):
        task = self.begin(); (self.workspace/'app.py').write_text('value=2\n',encoding='utf-8')
        self.verified(task)
        self.call('usage', task_id=task, usage={'totalTokens':20,'cost':{'total':0.03},'thinking':'do not store'})
        response = self.call('finish', task_id=task, state='completed', elapsed_ms=2500)
        card = response['completion_card']
        self.assertEqual(card['decision'], 'completed'); self.assertEqual(card['changed_files'], ['app.py'])
        self.assertEqual(card['elapsed_seconds'], 2.5); self.assertEqual(card['reported_cost'], 0.03)
        self.assertEqual(card['tokens'], 20); self.assertEqual(card['model']['id'],'offline')
        self.assertFalse(card['automatic_upgrade']); self.assertTrue(card['evidence_ids'])
        page = Path(response['card_html']).read_text(encoding='utf-8')
        self.assertNotIn('<script>alert(1)',page); self.assertIn('&lt;script&gt;',page)
        self.assertIn(Path(response['evidence_path']).name,page)
        self.assertNotIn('do not store',Path(response['evidence_path']).read_text(encoding='utf-8'))
        evidence = json.loads(Path(response['evidence_path']).read_text(encoding='utf-8'))
        self.assertEqual(evidence['task']['id'], task)
        (self.workspace/'app.py').write_text('value=3\n',encoding='utf-8')
        stale = self.call('task-card', task_id=task)
        self.assertEqual(stale['completion_card']['decision'], 'needs_verification')
        self.assertNotEqual(stale['card_html'], response['card_html'])

    def test_raw_old_verification_does_not_crash_or_claim_freshness(self):
        task = self.begin()
        self.e.record(task,'verification',{'status':'passed','test_count':1,'protected_intact':True})
        response=self.call('finish',task_id=task,state='completed')
        self.assertEqual(response['completion_card']['decision'],'needs_verification')
        self.assertEqual(response['completion_card']['verification']['freshness'],'not_checked')

    def test_unknown_protection_and_violations_have_separate_actions(self):
        task = self.begin(); self.verified(task, protected_intact=None)
        self.call('finish',task_id=task,state='completed')
        self.assertEqual(build(self.s,'alice','p',task)[0]['decision'],'needs_protection_check')
        self.verified(task,protected_intact=False,integrity_ok=False,violations=[{'path':'tests/test_old.py','reason':'changed'}])
        card=build(self.s,'alice','p',task)[0]
        self.assertEqual(card['decision'],'needs_fix'); self.assertTrue(card['review_signal'])

    def test_partial_usage_is_unknown_not_zero_or_partial_total(self):
        task=self.begin()
        self.call('usage',task_id=task,usage={'totalTokens':10,'cost':{'total':0.1}})
        self.call('usage',task_id=task,usage={'totalTokens':5})
        card=self.call('finish',task_id=task,state='aborted')['completion_card']
        self.assertIsNone(card['reported_cost']); self.assertEqual(card['known_reported_cost'],0.1)
        self.assertEqual(card['missing_cost_messages'],1); self.assertEqual(card['decision'],'needs_attention')

    def test_selected_skill_version_is_resolved_by_id_and_scope(self):
        task=self.begin(); registry=Components(self.s,'alice','p')
        payload={'kind':'skill','description':'Original','steps':['Run tests'],'triggers':['test']}
        old=registry.propose('skill:verify',payload,[],reason='fixture',seed=True)
        registry.propose('skill:verify',payload|{'description':'Later'},[],reason='later fixture')
        self.e.record(task,'context_assembled',{'workspace':str(self.workspace),'skill_ids':[old['id']]})
        self.call('finish',task_id=task,state='completed')
        card=build(self.s,'alice','p',task)[0]
        self.assertEqual(card['skills'][0]['version'],1)
        with self.assertRaises(ValueError):export(self.s,'bob','p',task)
        with self.assertRaises(ValueError):self.call('usage',task_id=task,usage={})


if __name__=='__main__':unittest.main()
