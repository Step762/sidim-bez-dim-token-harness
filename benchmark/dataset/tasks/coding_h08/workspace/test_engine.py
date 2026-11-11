import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.rank(*([2, 7, 1],)), [7, 2, 1])

if __name__ == '__main__':
    unittest.main()
