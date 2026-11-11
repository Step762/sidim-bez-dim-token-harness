import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.square(*(3,)), 9)
        self.assertEqual(lib.square(*(-2,)), 4)

if __name__ == '__main__':
    unittest.main()
