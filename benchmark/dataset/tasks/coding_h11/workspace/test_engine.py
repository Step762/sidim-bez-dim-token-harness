import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.distance(*(8, 3)), 5)
        self.assertEqual(lib.distance(*(2, 9)), 7)

if __name__ == '__main__':
    unittest.main()
