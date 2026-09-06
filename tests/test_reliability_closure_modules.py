"""No platform process or network calls: the codesign runner is always mocked."""
import subprocess
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl, urlsplit

from core.monitor_result import classify_monitor_result, monitor_status
from core.wechat_signature import inspect_wechat_signature
from core.url_safety import redact_url_for_display, redact_urls_in_text


class CompoundCredentialTests(unittest.TestCase):
    def test_compound_credentials_are_redacted(self):
        for key in (
            'download_token', 'shareToken', 'JWTToken', 'jwt_token', 'my_secret',
            'my-secret', 'downloadtoken', 'sharetoken', 'mysecret', 'token2',
            'download_token2', 'myJWT', 'token.download', 'private[secret]',
        ):
            with self.subTest(key=key):
                result = redact_url_for_display(f'https://example.test/file?{key}=PRIVATE_SENTINEL&lang=zh')
                self.assertNotIn('PRIVATE_SENTINEL', result)
                self.assertIn('lang=zh', result)

    def test_normal_words_and_exact_private_input_are_unchanged(self):
        original = 'https://example.test/?tokenization=paper&secretary=person&monkey=animal'
        self.assertEqual(redact_url_for_display(original), original)

    def test_encoded_keys_duplicates_and_fragment_are_redacted(self):
        original = 'https://example.test/?share%54oken=A&shareToken=B#route?my_secret=C'
        redacted = redact_url_for_display(original)
        self.assertEqual(parse_qsl(urlsplit(redacted).query),
                         [('shareToken', 'REDACTED'), ('shareToken', 'REDACTED')])
        self.assertEqual(urlsplit(redacted).fragment, 'route?my_secret=REDACTED')
        self.assertIn('share%54oken=A', original)

    def test_text_projection_redaction_is_idempotent(self):
        original = '文件 https://example.test/?download_token=PRIVATE_SENTINEL'
        redacted = redact_urls_in_text(original)
        self.assertNotIn('PRIVATE_SENTINEL', redacted)
        self.assertEqual(redact_urls_in_text(redacted), redacted)


class SignaturePredicateTests(unittest.TestCase):
    @staticmethod
    def runner(detail, *, verify_code=0, display_code=0):
        return Mock(side_effect=[
            subprocess.CompletedProcess([], verify_code, '', ''),
            subprocess.CompletedProcess([], display_code, '', detail),
        ])

    def test_valid_signature_uses_exact_target_twice_without_mutation(self):
        path = '/fixture/runtime directory/WeChat.app'
        runner = self.runner('CodeDirectory v=20400 flags=0x2(adhoc) hashes=14+2\nSignature=adhoc\n')
        result = inspect_wechat_signature(path, runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(len(runner.call_args_list), 2)
        for call in runner.call_args_list:
            args = call.args[0]
            self.assertEqual(args[-1], path)
            self.assertNotIn('sudo', args)
            self.assertNotIn('--sign', args)
            self.assertIn('timeout', call.kwargs)
        self.assertIn('--strict', runner.call_args_list[0].args[0])

    def test_failed_strict_verify_never_accepts_display_only_flags(self):
        runner = self.runner('CodeDirectory flags=0x2(adhoc)\nSignature=adhoc\n', verify_code=1)
        self.assertEqual(inspect_wechat_signature('/fixture/WeChat.app', runner=runner).code,
                         'wechat_signature_invalid')
        runner.assert_called_once()

    def test_runtime_bit_without_text_label_is_rejected(self):
        runner = self.runner('CodeDirectory flags=0x10002\nSignature=adhoc\n')
        self.assertEqual(inspect_wechat_signature('/fixture/WeChat.app', runner=runner).code,
                         'wechat_hardened_runtime_still_enabled')

    def test_unrelated_runtime_text_does_not_reject_valid_signature(self):
        runner = self.runner('Executable=/fixture/runtime/WeChat\nCodeDirectory flags=0x2(adhoc)\nSignature=adhoc\n')
        self.assertTrue(inspect_wechat_signature('/fixture/WeChat.app', runner=runner).ok)

    def test_missing_malformed_or_contradictory_evidence_fails_closed(self):
        for detail in (
            'Signature=adhoc\n',
            'CodeDirectory flags=garbage\nSignature=adhoc\n',
            'CodeDirectory flags=0x2(adhoc)\nSignature=adhoc\nSignature=other\n',
            'CodeDirectory flags=0x2(adhoc)\nCodeDirectory flags=0x10002\nSignature=adhoc\n',
        ):
            with self.subTest(detail=detail):
                self.assertFalse(inspect_wechat_signature('/fixture/WeChat.app', runner=self.runner(detail)).ok)
        with patch(
            'core.wechat_signing_identity.managed_certificate_root_hash',
            return_value=None,
        ):
            detail = 'CodeDirectory flags=0x2(adhoc)\nSignature size=500\n'
            self.assertFalse(inspect_wechat_signature('/fixture/WeChat.app', runner=self.runner(detail)).ok)

    def test_managed_stable_identity_is_accepted(self):
        path = '/fixture/runtime directory/WeChat.app'
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess(
                [], 0, '',
                'CodeDirectory v=20500 size=755 flags=0x0(none) hashes=14+2 location=embedded\n'
                'Signature size=1840\n',
            ),
            subprocess.CompletedProcess(
                [], 0, '',
                'Executable=/fixture/WeChat\n'
                'designated => identifier "com.tencent.xinWeChat" and '
                'certificate root = H"0123abcdef"\n',
            ),
        ])
        result = inspect_wechat_signature(
            path, runner=runner, expected_certificate_root='0123ABCDEF',
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(runner.call_args_list), 3)
        for call in runner.call_args_list:
            self.assertNotIn('sudo', call.args[0])
            self.assertNotIn('--sign', call.args[0])

    def test_foreign_certificate_is_rejected(self):
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, '', ''),
            subprocess.CompletedProcess(
                [], 0, '',
                'CodeDirectory v=20500 size=755 flags=0x0(none) hashes=14+2 location=embedded\n'
                'Signature size=8979\n',
            ),
            subprocess.CompletedProcess(
                [], 0, '',
                'designated => identifier "com.tencent.xinWeChat" and anchor apple generic\n',
            ),
        ])
        result = inspect_wechat_signature(
            '/fixture/WeChat.app', runner=runner,
            expected_certificate_root='0123abcdef',
        )
        self.assertEqual(result.code, 'wechat_signature_not_stable_identity')

    def test_adhoc_is_rejected_when_stable_identity_is_required(self):
        runner = self.runner('CodeDirectory flags=0x2(adhoc)\nSignature=adhoc\n')
        result = inspect_wechat_signature(
            '/fixture/WeChat.app', runner=runner,
            expected_certificate_root='0123abcdef',
        )
        self.assertEqual(result.code, 'wechat_signature_not_stable_identity')
        self.assertEqual(len(runner.call_args_list), 2)

    def test_certificate_signature_without_managed_identity_fails_closed(self):
        runner = self.runner(
            'CodeDirectory flags=0x0(none)\nSignature size=1840\n'
        )
        with patch(
            'core.wechat_signing_identity.managed_certificate_root_hash',
            return_value=None,
        ):
            result = inspect_wechat_signature('/fixture/WeChat.app', runner=runner)
        self.assertEqual(result.code, 'wechat_signature_not_stable_identity')

    def test_failed_display_is_not_success(self):
        runner = self.runner('CodeDirectory flags=0x2(adhoc)\nSignature=adhoc\n', display_code=1)
        self.assertFalse(inspect_wechat_signature('/fixture/WeChat.app', runner=runner).ok)

    def test_timeout_and_absent_binary_do_not_expose_exception_text(self):
        for error in (subprocess.TimeoutExpired('PRIVATE_SENTINEL', 60),
                      FileNotFoundError('PRIVATE_SENTINEL')):
            with self.subTest(kind=type(error).__name__):
                result = inspect_wechat_signature('/fixture/WeChat.app', runner=Mock(side_effect=error))
                self.assertFalse(result.ok)
                self.assertNotIn('PRIVATE_SENTINEL', repr(result))

    def test_invalid_target_makes_no_process_call(self):
        for path in ('', None, '/bad\0path'):
            with self.subTest(path=path):
                runner = Mock()
                self.assertFalse(inspect_wechat_signature(path, runner=runner).ok)
                runner.assert_not_called()


class MonitorOutcomeTests(unittest.TestCase):
    def test_only_existing_post_commit_codes_count_as_progress(self):
        for code in ('no_match', 'notified', 'duplicate', 'cooldown', 'source_advanced_no_visible'):
            with self.subTest(code=code):
                self.assertEqual(classify_monitor_result({'status': code}).action, 'progress')

    def test_new_and_known_failures_stop_without_a_negative_allowlist(self):
        for code in (
            'source_generation_admission_required', 'source_page_invalid',
            'source_cursor_stalled', 'source_envelope_invalid',
            'knowledge_recovery_unavailable', 'ai_backoff', 'initialized',
            'monitor_state_conflict', 'source_message_decode_failed', 'future_stop_state',
        ):
            with self.subTest(code=code):
                outcome = classify_monitor_result({'status': code, 'source_eof': True})
                self.assertEqual((outcome.action, outcome.reason), ('blocked', code))

    def test_only_boolean_true_eof_completes(self):
        self.assertEqual(classify_monitor_result({'status': 'no_messages', 'source_eof': True}).action, 'complete')
        for eof in (False, None, 1, 'true', {}):
            with self.subTest(eof=eof):
                result = classify_monitor_result({'status': 'no_messages', 'source_eof': eof})
                self.assertEqual((result.action, result.reason), ('blocked', 'source_eof_unverified'))

    def test_invalid_payload_and_status_are_bounded_content_free_stops(self):
        for result in (None, [], 'PRIVATE_SENTINEL', {'status': 10},
                       {'status': 'source_error: PRIVATE_SENTINEL'}, {'status': 'x' * 81}):
            with self.subTest(result=result):
                outcome = classify_monitor_result(result)
                self.assertEqual(outcome.action, 'blocked')
                self.assertNotIn('PRIVATE_SENTINEL', repr(outcome))
        self.assertEqual(monitor_status('future_safe_code'), 'future_safe_code')


if __name__ == '__main__':
    unittest.main()
