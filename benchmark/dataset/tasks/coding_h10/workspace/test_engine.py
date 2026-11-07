import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.clamp(*(-3,)), 0)
        self.assertEqual(lib.clamp(*(15,)), 10)
        self.assertEqual(lib.clamp(*(4,)), 4)

if __name__ == '__main__':
    unittest.main()
