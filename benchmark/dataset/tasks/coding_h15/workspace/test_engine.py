import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.f(*(3,)), 3)

if __name__ == '__main__':
    unittest.main()
