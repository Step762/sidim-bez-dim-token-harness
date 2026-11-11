import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.unique(*([3, 1, 3],)), [1, 3])
        self.assertEqual(lib.unique(*([],)), [])

if __name__ == '__main__':
    unittest.main()
