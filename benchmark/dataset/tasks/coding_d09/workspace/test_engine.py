import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.magnitude(*(-4,)), 4)
        self.assertEqual(lib.magnitude(*(3,)), 3)

if __name__ == '__main__':
    unittest.main()
