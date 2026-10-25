import json
import tempfile
from pathlib import Path
import unittest

from evolving_harness.completion import export
from evolving_harness.evidence import snapshot
from evolving_harness.native import dispatch
from evolving_harness.store import Store
from evolving_harness.task_feedback import TaskFeedback


class TaskFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.s=Store(self.root/'state.db')
        task=dispatch(self.s,'alice','p',{'operation':'begin','prompt':'Add filter','workspace':str(self.root)})
        self.task=task['task_id'];self.s.finish_task(self.task,'completed','model claims tests passed')
        self.f=TaskFeedback(self.s,'alice','p')

    def tearDown(self):self.s.close();self.temp.cleanup()

    def submit(self,rating='adjust',target='task',note='还需要中文示例',request='one'):
        return self.f.submit(self.task,rating,target,note,request)

    def test_satisfaction_is_no_change_not_gain_or_memory(self):
        before=self.s.db.execute('select count(*) from memories').fetchone()[0]
        result=self.submit('satisfied','task','')
        suggestion=result['suggestions'][0]
        self.assertEqual(suggestion['status'],'no_change');self.assertEqual(suggestion['target'],'none')
        self.assertEqual(before,self.s.db.execute('select count(*) from memories').fetchone()[0])
        card=export(self.s,'alice','p',self.task)['completion_card']
        self.assertEqual(card['user_outcome'],'satisfied')
        self.assertFalse(card['review_signal'])
        self.assertEqual(card['decision'],'needs_verification')
        with self.assertRaises(ValueError):self.submit('satisfied','workflow','',request='bad')

    def test_adjustment_cites_feedback_and_passed_test_without_overwriting_it(self):
        current=snapshot(str(self.root))
        with self.s.db:self.s.db.execute('insert into events(owner,project,task_id,kind,data,created) values(?,?,?,?,?,?)',
            ('alice','p',self.task,'verification',json.dumps({'status':'passed','test_count':1,'cwd':str(self.root),
              'after_snapshot':current,'protected_intact':True}),'fixture'))
        result=self.submit('issue','workflow','用户希望错误提示更清楚')
        suggestion=result['suggestions'][0]
        self.assertEqual(suggestion['status'],'pending');self.assertEqual(len(suggestion['evidence_ids']),2)
        self.assertIn('passed',suggestion['reason']);self.assertIn('假设',suggestion['proposal'])
        events=self.s.events('alice','p')
        self.assertTrue(set(suggestion['evidence_ids'])<=set(e['seq'] for e in events if e['task_id']==self.task))
        self.assertEqual([e for e in events if e['kind']=='verification'][-1]['data']['status'],'passed')

    def test_review_reject_and_stale_revision_never_apply(self):
        result=self.submit(target='memory');key=result['suggestions'][0]['id']
        active=[tuple(r) for r in self.s.db.execute('select * from active')]
        decided=self.f.decide(key,1,'review')['suggestions'][0]
        self.assertEqual(decided['status'],'reviewed_not_applied');self.assertEqual(decided['revision'],2)
        self.assertEqual(self.s.db.execute('select count(*) from memories').fetchone()[0],0)
        self.assertEqual(active,[tuple(r) for r in self.s.db.execute('select * from active')])
        with self.assertRaises(ValueError):self.f.decide(key,1,'reject')
        next_result=self.submit(request='two');key=next_result['suggestions'][-1]['id']
        self.assertEqual(self.f.decide(key,1,'reject')['suggestions'][-1]['status'],'rejected')

    def test_idempotency_scope_and_confirmation(self):
        first=self.submit();second=self.submit()
        self.assertEqual(first,second)
        with self.assertRaises(ValueError):self.submit(note='different')
        with self.assertRaises(ValueError):TaskFeedback(self.s,'bob','p').list(self.task)
        with self.assertRaises(ValueError):TaskFeedback(self.s,'bob','p').decide(first['suggestions'][0]['id'],1,'review')
        with self.assertRaises(ValueError):dispatch(self.s,'alice','p',{'operation':'task-rating-submit','task_id':self.task})
        with self.assertRaises(ValueError):self.submit(note='',request='empty')

    def test_html_feedback_form_and_untrusted_text_are_safe(self):
        self.submit(note='</script><img src=x onerror=alert(1)>')
        result=export(self.s,'alice','p',self.task)
        page=Path(result['card_html']).read_text(encoding='utf-8')
        self.assertIn('data-rating="satisfied"',page);self.assertIn('data-rating="adjust"',page)
        self.assertIn('data-rating="issue"',page);self.assertIn('/harness rate ',page)
        self.assertNotIn('<img src=x',page);self.assertIn('&lt;img',page)
        self.assertIn(self.task,page);self.assertFalse(result['completion_card']['task_feedback']['automatic_application'])

    def test_generated_card_does_not_change_workspace_fingerprint(self):
        before=snapshot(str(self.root))
        export(self.s,'alice','p',self.task)
        after=snapshot(str(self.root))
        self.assertEqual(before['manifest_hash'],after['manifest_hash'])


if __name__=='__main__':unittest.main()
