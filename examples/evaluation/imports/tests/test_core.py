import unittest
from toolkit.core import parse
class Tests(unittest.TestCase):
 def test_existing(self):self.assertEqual(parse('42'),42)
