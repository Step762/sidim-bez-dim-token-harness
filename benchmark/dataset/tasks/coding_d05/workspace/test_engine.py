import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.even(*(4,)), True)
        self.assertEqual(lib.even(*(9,)), False)

if __name__ == '__main__':
    unittest.main()
