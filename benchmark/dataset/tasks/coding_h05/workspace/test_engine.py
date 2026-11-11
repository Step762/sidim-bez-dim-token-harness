import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.cube(*(3,)), 27)
        self.assertEqual(lib.cube(*(-2,)), -8)

if __name__ == '__main__':
    unittest.main()
