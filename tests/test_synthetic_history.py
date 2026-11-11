"""Publishable fixture provenance and strict separation from LIVE authorization."""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from diagnostics import a1_resume
from synthetic_history import fixtures,encode,patch_history,ACK_USAGE,MATH_USAGE


class SyntheticHistoryTests(unittest.TestCase):
    def test_files_match_independent_constructor_and_hash_chain(self):
        records=fixtures()
        for name,value in records.items():
            self.assertEqual((Path(__file__).parent/'fixtures'/name).read_bytes(),encode(value))
            self.assertTrue(value['synthetic_fixture'])
        sha=hashlib.sha256(encode(records['a1_ack_synthetic.json'])).hexdigest()
        self.assertEqual(records['a1_math_synthetic.json']['source_sha256'],sha)
        self.assertEqual(records['a1_math_intent_synthetic.json']['source_sha256'],sha)
        self.assertEqual(ACK_USAGE,(17,18))
        self.assertEqual(MATH_USAGE,(29,9))

    def test_unpatched_live_validator_rejects_synthetic_source_before_transport(self):
        path=Path(__file__).parent/'fixtures/a1_ack_synthetic.json'
        with patch('httpx.AsyncHTTPTransport',side_effect=AssertionError('no real HTTP')):
            with self.assertRaisesRegex(a1_resume.ResumeBlocked,'receipt hash mismatch'):
                a1_resume.validate_source(path)

    def test_test_pins_restore_and_no_payload_response_headers_or_timestamps_exist(self):
        before=a1_resume.SOURCE_SHA256,a1_resume.BASE_URL,a1_resume.ACK_USAGE
        with patch_history():
            self.assertNotEqual(a1_resume.SOURCE_SHA256,before[0])
            self.assertEqual(a1_resume.BASE_URL,'https://synthetic-a1.invalid/v1')
        self.assertEqual((a1_resume.SOURCE_SHA256,a1_resume.BASE_URL,a1_resume.ACK_USAGE),before)
        def walk(value):
            if isinstance(value,dict):
                for key,child in value.items():
                    self.assertNotIn(key,{'payload','content','messages','Authorization','api_key',
                                          'response','timestamp','created','events'})
                    walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
        walk(fixtures())

if __name__=='__main__':unittest.main()
