import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.factorial(*(0,)), 1)
        self.assertEqual(lib.factorial(*(4,)), 24)

if __name__ == '__main__':
    unittest.main()
