import tempfile
from pathlib import Path
import unittest

from evolving_harness.native import dispatch
from evolving_harness.store import Store


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.s=Store(self.root/'.harness/state.sqlite3')

    def tearDown(self):self.s.close();self.temp.cleanup()

    def call(self,owner='alice',project='p'):
        return dispatch(self.s,owner,project,{'operation':'home','workspace':str(self.root)})

    def test_new_project_has_clear_first_action_without_creating_model_task(self):
        result=self.call();home=result['project_home']
        self.assertIsNone(home['recent_task']);self.assertIsNone(home['latest_card'])
        self.assertEqual(self.s.db.execute('select count(*) from tasks').fetchone()[0],0)
        self.assertTrue(home['daily']);self.assertTrue(home['experimental'])
        self.assertIn('未证明',home['experimental'][0]['status'])
        page=Path(result['guide_html']).read_text(encoding='utf-8')
        self.assertIn('暂无任务',page);self.assertIn('/harness card full',page)

    def test_recent_task_links_to_evidence_card_but_other_scope_has_none(self):
        task=dispatch(self.s,'alice','p',{'operation':'begin','prompt':'Fix parser','workspace':str(self.root)})['task_id']
        dispatch(self.s,'alice','p',{'operation':'finish','task_id':task,'state':'completed','text':'tests passed claim'})
        result=self.call();home=result['project_home']
        self.assertEqual(home['recent_task']['id'],task)
        self.assertTrue(Path(home['latest_card']).is_file())
        self.assertIn('查看最近任务卡片',Path(result['guide_html']).read_text(encoding='utf-8'))
        self.assertIsNone(self.call('bob')['project_home']['recent_task'])
        self.assertIsNone(self.call(project='other')['project_home']['recent_task'])
        self.assertEqual(self.s.get_task('alice','p',task)['state'],'completed')

    def test_html_escapes_scope_names(self):
        result=self.call(project='<script>alert(1)</script>')
        text=Path(result['guide_html']).read_text(encoding='utf-8')
        self.assertNotIn('<script>',text);self.assertIn('&lt;script&gt;',text)


if __name__=='__main__':unittest.main()
