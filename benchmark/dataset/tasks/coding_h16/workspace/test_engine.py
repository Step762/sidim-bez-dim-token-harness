import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.prefix(*([1, 2, 3], 2)), [1, 2])
        self.assertEqual(lib.prefix(*([1], 0)), [])

if __name__ == '__main__':
    unittest.main()
