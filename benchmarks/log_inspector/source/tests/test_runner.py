import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class RunnerTests(unittest.TestCase):
    def run_fixture(self,test_source):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'checks').mkdir();(root/'tests').mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1]/'checks'/'run_tests.py',root/'checks'/'run_tests.py')
            (root/'sample.py').write_text('answer=42\n',encoding='utf-8')
            if test_source is not None:(root/'tests'/'test_sample.py').write_text(test_source,encoding='utf-8')
            env=os.environ.copy();env.pop('PYTHONPATH',None)
            return subprocess.run([sys.executable,'-I','-X','utf8','checks/run_tests.py'],cwd=root,env=env,
                                  capture_output=True,text=True,encoding='utf-8',timeout=10)

    def test_imports_project_without_pythonpath(self):
        r=self.run_fixture('import unittest\nfrom sample import answer\nclass Example(unittest.TestCase):\n def test_answer(self):self.assertEqual(answer,42)\n')
        self.assertEqual(r.returncode,0,r.stderr);self.assertIn('Ran 1 test',r.stderr)

    def test_actual_failure_returns_nonzero(self):
        r=self.run_fixture('import unittest\nclass Example(unittest.TestCase):\n def test_failure(self):self.fail("fixture failure")\n')
        self.assertNotEqual(r.returncode,0);self.assertIn('FAILED',r.stderr)

    def test_empty_suite_returns_nonzero(self):
        r=self.run_fixture(None)
        self.assertNotEqual(r.returncode,0);self.assertIn('Ran 0 tests',r.stderr)
