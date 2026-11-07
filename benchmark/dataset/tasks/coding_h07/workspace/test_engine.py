import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.positive(*([-2, 0, 3],)), [3])
        self.assertEqual(lib.positive(*([],)), [])

if __name__ == '__main__':
    unittest.main()
