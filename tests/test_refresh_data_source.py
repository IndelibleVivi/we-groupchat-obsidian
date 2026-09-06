import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch
from core.key_extractor import KeyRecoveryResult
from scripts.refresh_data_source import main, refresh_data_source


class RefreshDataSourceTests(unittest.TestCase):
    def run_result(self, outcome):
        with patch('scripts.refresh_data_source.process_lookup_available', return_value=True), \
             patch('scripts.refresh_data_source.recover_keys', return_value=outcome):
            return refresh_data_source()

    def test_fresh_verified_observed_keys_succeed(self):
        result=self.run_result(KeyRecoveryResult('fresh_verified',{'message/message_0.db':{'enc_key':'12'*32}},1))
        self.assertTrue(result.ok)
        self.assertEqual(result.key_count,1)
        self.assertEqual(result.missing_databases,[])
        self.assertIn('Source inventory',result.message)

    def test_partial_does_not_claim_refresh_success(self):
        result=self.run_result(KeyRecoveryResult('partial',{'message/message_0.db':{'enc_key':'12'*32}},1,('message/message_1.db',)))
        self.assertFalse(result.ok)
        self.assertEqual(result.key_count,1)
        self.assertEqual(len(result.missing_databases),1)

    def test_cache_only_is_failure_for_a_refresh(self):
        result=self.run_result(KeyRecoveryResult('cache_only',reason='no_fresh_verified_keys'))
        self.assertFalse(result.ok)
        self.assertEqual(result.status,'cache_only')

    def test_unsupported_build_is_not_fresh_success(self):
        self.assertFalse(self.run_result(KeyRecoveryResult('unsupported_build')).ok)

    def test_process_lookup_unknown_does_not_attempt_recovery(self):
        with patch('scripts.refresh_data_source.process_lookup_available',return_value=False), \
             patch('scripts.refresh_data_source.recover_keys') as recover:
            result=refresh_data_source()
        self.assertFalse(result.ok)
        self.assertIn('无法检测',result.message)
        recover.assert_not_called()

    def test_empty_fresh_result_cannot_claim_success(self):
        self.assertFalse(self.run_result(KeyRecoveryResult('fresh_verified',verified_count=1)).ok)

    def test_cli_requires_explicit_transient_source_consent_before_refresh(self):
        stderr = StringIO()
        with patch("scripts.refresh_data_source.refresh_data_source") as refresh, \
             redirect_stderr(stderr):
            code = main([])

        self.assertEqual(code, 2)
        refresh.assert_not_called()
        self.assertIn("--allow-transient-wechat-source-read", stderr.getvalue())

    def test_cli_explicit_transient_source_consent_runs_refresh(self):
        outcome = type(
            "Outcome",
            (),
            {
                "ok": True,
                "status": "fresh_verified",
                "message": "verified",
                "reason": "",
                "key_count": 1,
                "missing_databases": [],
            },
        )()
        with patch(
            "scripts.refresh_data_source.refresh_data_source",
            return_value=outcome,
        ) as refresh:
            code = main(["--allow-transient-wechat-source-read"])

        self.assertEqual(code, 0)
        refresh.assert_called_once_with()
