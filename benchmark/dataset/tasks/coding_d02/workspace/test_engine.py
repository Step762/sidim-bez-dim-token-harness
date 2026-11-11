import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.product(*(8, 2)), 16)
        self.assertEqual(lib.product(*(-3, 4)), -12)

if __name__ == '__main__':
    unittest.main()
