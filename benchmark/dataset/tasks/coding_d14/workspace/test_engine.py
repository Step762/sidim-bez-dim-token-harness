import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.double(*(3,)), 6)

if __name__ == '__main__':
    unittest.main()
