import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.odd(*(3,)), True)
        self.assertEqual(lib.odd(*(8,)), False)

if __name__ == '__main__':
    unittest.main()
