import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.peak(*([2, 8, 3],)), 8)
        self.assertEqual(lib.peak(*([-7, -2],)), -2)

if __name__ == '__main__':
    unittest.main()
