from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai.cloudflare_provider import CloudflareClient, CloudflareDecisionProvider, CloudflareEmbeddingProvider, HTTPResponse
from ai.semantic_provider import (
    DecisionQuestion, DecisionRequest, EmbeddingProfile, LegacyTextProvider,
    ProviderError, TextDecisionProvider, parse_decision_answers,
)
from core.provider_usage import ProviderUsageStore
from tests.test_provider_usage import DAY, NOW


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        return self.handler(self.calls[-1])


def envelope(result, success=True):
    return HTTPResponse(200, json.dumps({"success": success, "errors": [], "result": result}).encode())


def decision_request():
    return DecisionRequest("Synthetic state", (
        DecisionQuestion("gate", "noul", "Is it relevant?"),
        DecisionQuestion("route", "choice", "Choose one", {"a": "A", "b": "B"}),
        DecisionQuestion("rank", "score", "Rate", ["low", "medium", "high"]),
    ))


def decision_body():
    return {"model": "clef-flash", "usage": {"input_tokens": 25, "output_tokens": 0}, "answers": {
        "gate": {"type": "noul", "noul": .8},
        "route": {"type": "choice", "choice": "b", "confidence": .6, "probabilities": {"a": .2, "b": .8}},
        "rank": {"type": "score", "score": 1.5, "confidence": .7, "probabilities": {"0": .1, "1": .3, "2": .6},
                 "legend": {"0": "low", "1": "medium", "2": "high"}},
    }}


class SemanticProviderTests(unittest.TestCase):
    def test_native_failure_logs_request_binding_and_safe_sdk_evidence(self):
        from ai.openai_provider import OpenAIProvider
        native = OpenAIProvider.__new__(OpenAIProvider)
        native.model, native.client = "synthetic-model", Mock()
        error = type("RateLimitError", (RuntimeError,), {})(
            'private error body sk-secret https://private.example/?token=secret')
        error.status_code, error.request_id = 429, "req_synthetic-provider-1"
        error.body = {"private": "message"}
        native.client.chat.completions.create.side_effect = error
        writer = LegacyTextProvider(native, provider_name="synthetic", model=native.model)
        request_id, generation_id = "job_" + "a" * 64 + ":text", "b" * 32
        with self.assertLogs("ai.semantic_provider", level="WARNING") as logs:
            with self.assertRaises(ProviderError) as failure:
                writer.generate("private prompt", request_id=request_id, generation_id=generation_id)
        self.assertEqual(failure.exception.code, "text_provider_failed")
        self.assertTrue(failure.exception.outcome_unknown)
        self.assertTrue(failure.exception.retryable)
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual(record, {
            "event": "semantic_text_provider_failure", "request_id": request_id,
            "generation_id": generation_id, "http_status": 429,
            "provider_request_id": "req_synthetic-provider-1",
        })
        for private in ("private", "sk-secret", "token=secret", "prompt", "body"):
            self.assertNotIn(private, json.dumps(record))
        native.client.chat.completions.create.assert_called_once()

    def test_failure_log_omits_untrusted_fields_and_stops_at_a_context_cycle(self):
        error = RuntimeError("private response")
        error.__context__ = error
        error.status_code = True
        writer = LegacyTextProvider(Mock(), provider_name="synthetic", model="synthetic")
        writer.provider.summarize.side_effect = error
        for value in ("https://private.example/?token=secret", "sk-secret", "req_bad\nheader", "req_" + "a" * 200):
            with self.subTest(value=value):
                error.request_id = value
                with self.assertLogs("ai.semantic_provider", level="WARNING") as logs:
                    with self.assertRaises(ProviderError) as failure:
                        writer.generate("private prompt", request_id=value, generation_id=value)
                self.assertTrue(failure.exception.outcome_unknown)
                self.assertEqual(json.loads(logs.records[0].getMessage()), {
                    "event": "semantic_text_provider_failure"})

    def test_broken_diagnostic_sink_cannot_change_unknown_failure(self):
        writer = LegacyTextProvider(Mock(), provider_name="synthetic", model="synthetic")
        writer.provider.summarize.side_effect = ConnectionError("private disconnect")
        with patch("ai.semantic_provider.logging.getLogger") as get_logger:
            get_logger.return_value.warning.side_effect = RuntimeError("broken diagnostic sink")
            with self.assertRaises(ProviderError) as failure:
                writer.generate("private prompt", request_id="job_" + "a" * 64 + ":text")
        self.assertEqual(failure.exception.code, "text_provider_failed")
        self.assertTrue(failure.exception.outcome_unknown)
        writer.provider.summarize.assert_called_once()

    def test_native_truncation_is_known_failure_not_unknown_or_accepted_text(self):
        from ai.openai_provider import OpenAIProvider
        for reason in ("length", "repetition_truncation"):
            with self.subTest(reason=reason):
                native = OpenAIProvider.__new__(OpenAIProvider)
                native.model, native.max_output_tokens = "synthetic-model", 16384
                native.client = Mock()
                native.client.chat.completions.create.return_value = SimpleNamespace(choices=[
                    SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content='{ "partial":'))])
                writer = LegacyTextProvider(native, provider_name="synthetic", model=native.model)
                with self.assertRaises(ProviderError) as error:
                    writer.generate("synthetic input", request_id="r1")
                self.assertEqual(error.exception.code, "text_output_truncated")
                self.assertFalse(error.exception.outcome_unknown)
                self.assertFalse(error.exception.retryable)
                native.client.chat.completions.create.assert_called_once()
                self.assertEqual(native.client.chat.completions.create.call_args.kwargs["max_tokens"], 16384)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProviderUsageStore(Path(self.tmp.name) / "usage.sqlite3", clock=lambda: NOW)

    def client(self, handler, *, confirmed=True, budget=10**12):
        transport = FakeTransport(handler)
        client = CloudflareClient("a" * 32, "synthetic-token", self.store, transport=transport)
        if confirmed:
            self.store.confirm_budget(client.account_key, utc_day=DAY, daily_limit_units=budget,
                                      external_usage_units=0, confirmation_id="synthetic-evidence", valid_until=NOW + 3600)
        return client, transport

    def test_clef_uses_typed_schema_and_real_route_with_usage_receipt(self):
        client, transport = self.client(lambda _call: envelope(decision_body()))
        result = CloudflareDecisionProvider(client).decide(decision_request(), request_id="d1", generation_id="g1")
        self.assertAlmostEqual(result.answers["rank"].score, 1.5)
        self.assertAlmostEqual(result.answers["gate"].noul, .8)
        self.assertEqual(result.route.model, "@cf/cloudflare/clef-flash")
        self.assertEqual(json.loads(transport.calls[0][3])["model"], "clef-flash")
        self.assertEqual(self.store.get("d1").charged_units, 25 * 3455)

    def test_question_identity_enum_and_probability_failures_never_become_no_match(self):
        mutations = [
            lambda b: b["answers"].pop("gate"),
            lambda b: b["answers"]["gate"].update(noul=True),
            lambda b: b["answers"]["gate"].update(noul=float("nan")),
            lambda b: b["answers"]["route"].update(choice="invented"),
            lambda b: b["answers"]["route"].update(probabilities={"a": .4, "b": .9}),
            lambda b: b["answers"]["route"].update(choice="a"),
            lambda b: b["answers"]["rank"].update(score=2.),
            lambda b: b["answers"]["rank"].update(legend={"0": "wrong", "1": "medium", "2": "high"}),
            lambda b: b["answers"]["gate"].update(text="invented body"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                body = decision_body()
                mutate(body)
                with self.assertRaises(ProviderError):
                    parse_decision_answers(body["answers"], decision_request())

    def test_question_bounds_and_no_silent_truncation(self):
        client, transport = self.client(lambda _call: self.fail("unexpected network"))
        provider = CloudflareDecisionProvider(client)
        for request in (
            DecisionRequest("state", tuple(DecisionQuestion(str(i), "noul", "Q") for i in range(65))),
            DecisionRequest("x" * 24576, decision_request().questions),
            DecisionRequest("state", (DecisionQuestion("same", "noul", "Q"), DecisionQuestion("same", "noul", "Q"))),
        ):
            with self.assertRaises(ProviderError):
                provider.decide(request, request_id="oversized")
        self.assertEqual(transport.calls, [])

    def test_canonical_redactor_covers_structured_keys_values_and_questions(self):
        client, transport = self.client(lambda _call: envelope({"model": "clef-flash", "usage": {"input_tokens": 3, "output_tokens": 0},
                                                               "answers": {"q": {"type": "noul", "noul": .2}}}))
        secret_url = "https://example.invalid/p?token=private-fixture"
        request = DecisionRequest({secret_url: [secret_url]}, (DecisionQuestion("q", "noul", secret_url),))
        CloudflareDecisionProvider(client).decide(request, request_id="redaction")
        body = transport.calls[0][3].decode()
        self.assertNotIn("private-fixture", body)
        self.assertIn("REDACTED", body)

    def test_false_success_or_malformed_envelope_is_not_success(self):
        bodies = [envelope(decision_body(), success=False), HTTPResponse(200, b'{"success":true,"result":null}'),
                  HTTPResponse(200, b'{"success":true,"success":false,"result":{}}')]
        for index, response in enumerate(bodies):
            client, _transport = self.client(lambda _call: response)
            with self.assertRaises(ProviderError):
                CloudflareDecisionProvider(client).decide(decision_request(), request_id=f"false{index}")
            self.assertEqual(self.store.get(f"false{index}").status, "unknown")

    def test_timeout_retains_bound_and_duplicate_id_never_resends(self):
        def fail(_call):
            raise TimeoutError("https://secret.invalid/?token=raw-token private prompt")
        client, transport = self.client(fail)
        for _ in range(2):
            with self.assertRaises(ProviderError) as result:
                CloudflareDecisionProvider(client).decide(decision_request(), request_id="timeout")
            self.assertNotIn("secret.invalid", str(result.exception))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.store.get("timeout").charged_units, 24576 * 3455)
        self.assertEqual(self.store.get("timeout").status, "unknown")

    def test_unconfirmed_account_does_not_call_transport(self):
        client, transport = self.client(lambda _call: envelope(decision_body()), confirmed=False)
        with self.assertRaisesRegex(ProviderError, "external_budget_unconfirmed"):
            CloudflareDecisionProvider(client).decide(decision_request(), request_id="blocked")
        self.assertEqual(transport.calls, [])

    def test_explicit_text_fallback_has_actual_route_and_same_validator(self):
        class Legacy:
            def summarize(self, prompt):
                self.prompt = prompt
                return json.dumps({"answers": decision_body()["answers"]})
        legacy = Legacy()
        self.store.confirm_budget("text-account", utc_day=DAY, daily_limit_units=100,
                                  external_usage_units=0, confirmation_id="text-evidence", valid_until=NOW + 3600)
        text = LegacyTextProvider(legacy, provider_name="configured", model="fixture-text",
                                  usage_store=self.store, account_key="text-account", upper_bound_units=10)
        result = TextDecisionProvider(text).decide(decision_request(), request_id="fallback")
        self.assertEqual(result.route.model, "fixture-text")
        self.assertEqual(result.route.fallback_from, "decision")
        legacy.summarize = lambda _prompt: '{"match":false}'
        with self.assertRaisesRegex(ProviderError, "decision_invalid_response"):
            TextDecisionProvider(text).decide(decision_request(), request_id="invalid")

    def test_configured_legacy_text_does_not_invent_cloudflare_budget_requirements(self):
        class Legacy:
            calls = 0
            def summarize(self, prompt):
                self.calls += 1
                return "Synthetic result"
        legacy = Legacy()
        remote = LegacyTextProvider(legacy, provider_name="configured", model="fixture-text")
        self.assertEqual(remote.generate("Prompt", request_id="configured-text").text, "Synthetic result")
        self.assertEqual(legacy.calls, 1)
        local = LegacyTextProvider(legacy, provider_name="ollama", model="fixture-text", local=True)
        self.assertEqual(local.generate("Prompt", request_id="local-text").text, "Synthetic result")
        self.assertEqual(legacy.calls, 2)

    def test_embedding_sorting_and_batching_restore_original_order(self):
        def respond(call):
            request = json.loads(call[3])
            self.assertIs(request["truncate_inputs"], False)
            data = []
            for text in request["text"]:
                vector = [0.] * 1024
                vector[len(text) % 1024] = 2.
                data.append(vector)
            return envelope({"data": data, "shape": [len(data), 1024], "pooling": "cls"})
        client, transport = self.client(respond)
        provider = CloudflareEmbeddingProvider(client, max_batch_size=2)
        texts = ["long text", "一", "mid", "z"]
        result = provider.embed(texts, purpose="document", request_id="embed")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual([vector.index(1.) for vector in result.vectors], [len(text) for text in texts])
        self.assertTrue(all(math.isclose(math.hypot(*vector), 1) for vector in result.vectors))

    def test_embedding_rejects_zero_wrong_dimension_nonfinite_and_indexed_output(self):
        data_cases = [[[0.] * 1024], [[1., 0.]], [[float("inf")] + [0.] * 1023],
                      [{"index": 1, "embedding": [1.] + [0.] * 1023}]]
        for index, data in enumerate(data_cases):
            client, _transport = self.client(lambda _call: envelope({"data": data, "shape": [1, 1024]}))
            with self.assertRaises(ProviderError):
                CloudflareEmbeddingProvider(client).embed(["text"], purpose="query", request_id=f"bad{index}")

    def test_entire_embedding_input_is_validated_before_any_request(self):
        client, transport = self.client(lambda _call: self.fail("unexpected request"))
        with self.assertRaisesRegex(ProviderError, "embedding_input_too_large"):
            CloudflareEmbeddingProvider(client).embed(["short", "长" * 3000], purpose="document", request_id="too-big")
        self.assertEqual(transport.calls, [])

    def test_entire_embedding_budget_is_reserved_before_any_request(self):
        client, transport = self.client(lambda _call: self.fail("unexpected request"), budget=70 * 1075)
        provider = CloudflareEmbeddingProvider(client, max_batch_size=1)
        with self.assertRaisesRegex(ProviderError, "usage_budget_exhausted"):
            provider.embed(["a", "b"], purpose="document", request_id="whole-budget")
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.store.day_usage(client.account_key, DAY), 0)

    def test_typed_validation_is_bound_to_dispatched_snapshot(self):
        request = decision_request()
        def respond(_call):
            request.questions[1].criteria.clear()
            return envelope(decision_body())
        client, _transport = self.client(respond)
        result = CloudflareDecisionProvider(client).decide(request, request_id="snapshot")
        self.assertEqual(result.answers["route"].choice, "b")

    def test_profile_binds_all_fields_even_when_dimensions_equal(self):
        original = EmbeddingProfile("test", "fixture", 3)
        replacements = dict(provider="other", model="other", pooling="mean", normalization="none",
                            document_template="Document: {text}", query_template="Query: {text}", chunker="other")
        for key, value in replacements.items():
            self.assertNotEqual(original.profile_id, replace(original, **{key: value}).profile_id)
        with self.assertRaises(ProviderError):
            replace(original, query_template="{text}{text}")


if __name__ == "__main__":
    unittest.main()
