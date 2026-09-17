import unittest

from core.api_errors import is_retryable_ai_error, normalize_ai_error


class NormalizeAIErrorTests(unittest.TestCase):
    def test_proxy_wrapped_credits_exhausted_is_quota_not_key_error(self):
        # 9router wraps an upstream 429 in a 503 envelope and prepends [AUTH]
        # account-lock lines; both must not be read as a bad API key.
        error = RuntimeError(
            'Error code: 503 - {"error":{"message":"[AUTH] codebuddy-intl | '
            "all 1 accounts locked for glm-5.2 (reset after 4m 11s) | "
            'lastError=[429]: {\\"error\\":{\\"data\\":{\\"code\\":14018,'
            '\\"msg\\":\\"Credits exhausted. Please visit the link below to '
            'purchase add-on packs and get more credits\\"}}}"}}'
        )
        self.assertEqual(
            normalize_ai_error(error, "custom"),
            "custom 额度已用完，请充值或等待额度重置后再试",
        )

    def test_insufficient_balance_is_quota_not_key_error(self):
        error = RuntimeError(
            'Error code: 401 - {"type":"error","error":{"type":"CreditsError",'
            '"message":"Insufficient balance. Manage your billing here: '
            'https://example.com/billing"}}'
        )
        self.assertEqual(
            normalize_ai_error(error),
            "AI 额度已用完，请充值或等待额度重置后再试",
        )

    def test_plain_401_still_reports_key_error(self):
        error = RuntimeError("Error code: 401 - invalid api key provided")
        self.assertEqual(
            normalize_ai_error(error),
            "AI API Key 无效或已过期，请在设置中重新配置",
        )

    def test_plain_429_still_reports_rate_limit(self):
        error = RuntimeError("Error code: 429 - rate limit reached")
        self.assertEqual(
            normalize_ai_error(error),
            "AI API 请求频率超限，请稍后再试",
        )

    def test_per_minute_quota_exceeded_reports_rate_limit(self):
        error = RuntimeError(
            "Error code: 429 - Quota exceeded for quota metric "
            "'Generate requests' and limit 'Requests per minute'"
        )
        self.assertEqual(
            normalize_ai_error(error),
            "AI API 请求频率超限，请稍后再试",
        )

    def test_explicit_credit_exhaustion_wins_over_429(self):
        error = RuntimeError(
            'Error code: 429 - {"error":{"code":"insufficient_quota",'
            '"message":"Quota exceeded. Check your billing details."}}'
        )
        self.assertEqual(
            normalize_ai_error(error),
            "AI 额度已用完，请充值或等待额度重置后再试",
        )

    def test_quota_exhaustion_is_not_briefly_retryable(self):
        self.assertFalse(is_retryable_ai_error(RuntimeError("Credits exhausted")))


if __name__ == "__main__":
    unittest.main()
