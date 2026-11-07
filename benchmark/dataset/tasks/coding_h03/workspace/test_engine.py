import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.net(*(20, 3)), 17)
        self.assertEqual(lib.net(*(5, 0)), 5)

if __name__ == '__main__':
    unittest.main()
