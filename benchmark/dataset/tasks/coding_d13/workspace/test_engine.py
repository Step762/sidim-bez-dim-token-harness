import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.tail(*([4, 8, 9],)), 9)
        self.assertEqual(lib.tail(*([2],)), 2)

if __name__ == '__main__':
    unittest.main()
