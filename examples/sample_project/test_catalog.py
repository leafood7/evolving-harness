import unittest

from catalog import paginate


class PaginationTests(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(paginate([1, 2, 3, 4, 5], 1, 2), [1, 2])

    def test_second_page(self):
        self.assertEqual(paginate([1, 2, 3, 4, 5], 2, 2), [3, 4])

    def test_end_and_empty(self):
        self.assertEqual(paginate([1, 2, 3, 4, 5], 3, 2), [5])
        self.assertEqual(paginate([], 1, 2), [])

    def test_invalid_input(self):
        for page, size in [(0, 2), (-1, 2), (1, 0), (1, -1)]:
            with self.subTest(page=page, size=size):
                with self.assertRaises(ValueError):
                    paginate([1, 2, 3], page, size)


if __name__ == "__main__":
    unittest.main()
