import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.indices(*(3,)), [0, 1, 2])
        self.assertEqual(lib.indices(*(0,)), [])

if __name__ == '__main__':
    unittest.main()
