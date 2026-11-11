import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.pure(*(2,)), 3)
        self.assertEqual(lib.pure(*(2,)), 4)

if __name__ == '__main__':
    unittest.main()
