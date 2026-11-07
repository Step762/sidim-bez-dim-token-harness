import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.lookup(*({}, 'x')), 0)
        self.assertEqual(lib.lookup(*({'x': 3}, 'x')), 3)

if __name__ == '__main__':
    unittest.main()
