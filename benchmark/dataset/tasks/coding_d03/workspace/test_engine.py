import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.small(*(3, 9)), 3)
        self.assertEqual(lib.small(*(-4, 2)), -4)

if __name__ == '__main__':
    unittest.main()
