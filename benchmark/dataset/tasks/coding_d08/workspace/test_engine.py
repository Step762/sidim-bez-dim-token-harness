import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.clean(*('MiXeD',)), 'mixed')
        self.assertEqual(lib.clean(*('',)), '')

if __name__ == '__main__':
    unittest.main()
