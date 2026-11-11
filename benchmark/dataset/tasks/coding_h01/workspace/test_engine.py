import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.total(*([2, 5, -1],)), 6)
        self.assertEqual(lib.total(*([],)), 0)

if __name__ == '__main__':
    unittest.main()
