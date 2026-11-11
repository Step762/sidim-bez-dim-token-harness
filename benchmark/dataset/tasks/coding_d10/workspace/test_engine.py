import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.mean(*([1, 2],)), 1.5)
        self.assertEqual(lib.mean(*([2, 4],)), 3)

if __name__ == '__main__':
    unittest.main()
