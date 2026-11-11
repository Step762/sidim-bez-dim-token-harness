import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.adult(*(18,)), True)
        self.assertEqual(lib.adult(*(17,)), False)

if __name__ == '__main__':
    unittest.main()
