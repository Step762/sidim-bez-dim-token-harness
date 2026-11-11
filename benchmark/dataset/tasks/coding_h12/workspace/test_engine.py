import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.words(*('a  b',)), ['a', 'b'])
        self.assertEqual(lib.words(*('',)), [])

if __name__ == '__main__':
    unittest.main()
