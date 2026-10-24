import unittest
from app import normalize
class Tests(unittest.TestCase):
 def test_existing(self):self.assertEqual(normalize('INFO'),'INFO')
