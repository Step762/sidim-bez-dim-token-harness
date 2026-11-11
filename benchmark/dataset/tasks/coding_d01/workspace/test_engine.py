import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.join(*(3, 7)), 10)
        self.assertEqual(lib.join(*(-2, 5)), 3)

if __name__ == '__main__':
    unittest.main()
