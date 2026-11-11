import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.flip(*([3, 1, 2],)), [2, 1, 3])
        self.assertEqual(lib.flip(*([],)), [])

if __name__ == '__main__':
    unittest.main()
