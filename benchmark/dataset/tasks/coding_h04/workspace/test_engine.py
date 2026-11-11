import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.scale(*(4, 3)), 12)
        self.assertEqual(lib.scale(*(-2, 5)), -10)

if __name__ == '__main__':
    unittest.main()
