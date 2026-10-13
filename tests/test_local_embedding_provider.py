from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai.cloudflare_provider import CloudflareEmbeddingProvider, HTTPResponse, default_bge_m3_profile
from ai.local_embedding_provider import LocalEmbeddingProvider, LoopbackTransport, loopback_endpoint
from ai.semantic_provider import DecisionQuestion, DecisionRequest, ProviderError
from ai.vector_index import LocalCosineIndex, VectorRecord
from core.semantic.contracts import SemanticStoreError
from core.semantic.retrieval import HybridRetriever
from core.semantic.runtime import PROFILE_SCHEMA, ProviderRoles, validate_profile
from core.semantic.store import SemanticStore
from tests.test_semantic_engine import capture
from tests.test_semantic_providers import FakeTransport


LOCAL = {"endpoint": "http://127.0.0.1:11434/v1/embeddings", "model": "synthetic-local-v1",
         "dimensions": 3, "context_window_tokens": 1024, "max_input_bytes": 900}
PROFILE = {"schema": PROFILE_SCHEMA, "decision": "text", "embedding": "local", "vector_index": "local",
           "local_embedding": LOCAL}


def reply(call, vectors=None, *, reverse=True):
    payload = json.loads(call[3])
    vectors = vectors or [[0., 2., 0.] if "隐藏" in text or "synonym" in text else [3., 0., 0.]
                          for text in payload["input"]]
    rows = [{"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)]
    if reverse:
        rows.reverse()
    return HTTPResponse(200, json.dumps({"object": "list", "model": payload["model"], "data": rows,
                                        "usage": {"prompt_tokens": 4, "total_tokens": 4}}).encode())


class LocalEmbeddingProviderTests(unittest.TestCase):
    def provider(self, handler=reply, **config):
        transport = FakeTransport(handler)
        return LocalEmbeddingProvider.from_config({**LOCAL, **config}, transport=transport), transport

    def test_length_batches_and_indexed_response_restore_original_order(self):
        provider, transport = self.provider(max_batch_size=2, max_batch_bytes=900)
        texts = ["长的隐藏细节" * 10, "a", "synonym", "普通"]
        result = provider.embed(texts, purpose="document", request_id="synthetic")
        self.assertEqual(result.vectors, ((0., 1., 0.), (1., 0., 0.), (0., 1., 0.), (1., 0., 0.)))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sorted(text for call in transport.calls for text in json.loads(call[3])["input"]), sorted(texts))
        self.assertTrue(all(json.loads(call[3])["encoding_format"] == "float" for call in transport.calls))
        self.assertTrue(all("Authorization" not in call[2] for call in transport.calls))
        self.assertEqual((result.route.provider, result.route.model, result.route.fallback_from),
                         ("local", LOCAL["model"], None))
        self.assertEqual(result.profile_id, provider.profile.profile_id)

    def test_padded_batch_bound_splits_without_changing_text(self):
        provider, transport = self.provider(max_input_bytes=20, max_batch_bytes=20)
        texts = ["x" * 12, "y", "z" * 12]
        provider.embed(texts, purpose="document", request_id="padded")
        self.assertEqual(len(transport.calls), 3)
        for call in transport.calls:
            batch = json.loads(call[3])["input"]
            self.assertLessEqual(max(len(text.encode()) for text in batch) * len(batch), 20)

    def test_whole_input_and_prepared_template_validate_before_sending(self):
        provider, transport = self.provider(lambda _call: self.fail("transport must not run"),
                                            max_input_bytes=10, document_template="D: {text}")
        for texts in (["ok", "x" * 8], ["ok", "\ud800"], ["ok", ""], "not-a-list"):
            with self.subTest(texts=repr(texts)):
                with self.assertRaises(ProviderError):
                    provider.embed(texts, purpose="document", request_id="invalid")
        self.assertEqual(transport.calls, [])

    def test_canonical_redactor_and_distinct_doc_query_templates(self):
        provider, transport = self.provider(document_template="document: {text}", query_template="query: {text}")
        value = "https://example.invalid/a?token=synthetic-secret"
        for purpose in ("document", "query"):
            provider.embed([value], purpose=purpose, request_id=purpose)
        for call, prefix in zip(transport.calls, ("document: ", "query: ")):
            body = json.loads(call[3])
            self.assertTrue(body["input"][0].startswith(prefix))
            self.assertNotIn("synthetic-secret", body["input"][0])
            self.assertIn("REDACTED", body["input"][0])

    def test_malformed_outputs_fail_instead_of_producing_empty_or_partial_vectors(self):
        base = json.loads(reply((None, None, None, json.dumps({"model": LOCAL["model"], "input": ["a", "b"]}).encode())).body)
        changes = [
            lambda value: value.update(model="another-model"),
            lambda value: value.update(error={"message": "raw private text"}),
            lambda value: value.update(object="wrong"),
            lambda value: value["data"].pop(),
            lambda value: value["data"][0].update(index=0),
            lambda value: value["data"][0].update(index=True),
            lambda value: value["data"][0].update(index=2),
            lambda value: value["data"][0].pop("index"),
            lambda value: value["data"][0].update(embedding=[0., 0., 0.]),
            lambda value: value["data"][0].update(embedding=[1., 0.]),
            lambda value: value["data"][0].update(embedding=[True, 0., 0.]),
            lambda value: value["data"][0].update(embedding=[float("inf"), 0., 0.]),
            lambda value: value["data"][0].update(embedding="base64-is-not-float"),
        ]
        for change in changes:
            value = deepcopy(base)
            change(value)
            provider, transport = self.provider(lambda _call: HTTPResponse(200, json.dumps(value).encode()))
            with self.assertRaises(ProviderError) as error:
                provider.embed(["a", "b"], purpose="document", request_id="bad")
            self.assertNotIn("private", str(error.exception))
            self.assertEqual(len(transport.calls), 1)
        provider, _transport = self.provider(lambda _call: HTTPResponse(200, b'{"model":"a","model":"b"}'))
        with self.assertRaisesRegex(ProviderError, "provider_invalid_json"):
            provider.embed(["a"], purpose="query", request_id="duplicate-json")

    def test_redirect_http_failure_timeout_never_switch_route_or_retry(self):
        for status in (301, 302, 307, 308, 400, 429, 503):
            provider, transport = self.provider(lambda _call: HTTPResponse(status, b"raw private provider body"))
            with self.assertRaises(ProviderError) as error:
                provider.embed(["input"], purpose="query", request_id="http")
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(error.exception.outcome_unknown)
            self.assertNotIn("private", str(error.exception))
            if status < 400:
                self.assertEqual(error.exception.code, "embedding_redirect_rejected")
        def timeout(_call):
            raise TimeoutError("private prompt http://secret.invalid")
        provider, transport = self.provider(timeout)
        with self.assertRaisesRegex(ProviderError, "provider_transport_unknown") as error:
            provider.embed(["input"], purpose="query", request_id="timeout")
        self.assertTrue(error.exception.outcome_unknown)
        self.assertEqual(len(transport.calls), 1)

    def test_native_ollama_explicitly_disables_truncation_and_restores_order(self):
        def native(call):
            payload = json.loads(call[3])
            self.assertIs(payload["truncate"], False)
            self.assertNotIn("encoding_format", payload)
            return HTTPResponse(200, json.dumps({"model": payload["model"], "embeddings":
                [[0, 1, 0] if "隐藏" in text else [1, 0, 0] for text in payload["input"]]}).encode())
        provider, transport = self.provider(native, api="ollama", endpoint="http://localhost:11434/api/embed")
        result = provider.embed(["很长的隐藏内容", "a"], purpose="document", request_id="native")
        self.assertEqual(result.vectors, ((0., 1., 0.), (1., 0., 0.)))
        self.assertEqual(transport.calls[0][1], "http://127.0.0.1:11434/api/embed")

    def test_context_capacity_and_api_endpoint_are_explicit(self):
        for change in ({"context_window_tokens": 64}, {"context_window_tokens": True},
                       {"max_input_bytes": 961}, {"api": "unknown"}, {"max_batch_bytes": 800},
                       {"api": "ollama"}, {"endpoint": "http://127.0.0.1:11434/api/embed"}):
            with self.assertRaises(ProviderError):
                LocalEmbeddingProvider.from_config({**LOCAL, **change})
        for field in ("model", "dimensions", "endpoint", "context_window_tokens"):
            with self.assertRaises(ProviderError):
                LocalEmbeddingProvider.from_config({key: value for key, value in LOCAL.items() if key != field})

    def test_native_bad_counts_and_vectors_are_rejected(self):
        for vectors in ([], [[1, 0, 0], [0, 1, 0]], [[0, 0, 0]], [[1, 0]], [[True, 0, 0]]):
            provider, _transport = self.provider(
                lambda _call: HTTPResponse(200, json.dumps({"model": LOCAL["model"], "embeddings": vectors}).encode()),
                api="ollama", endpoint="http://127.0.0.1:11434/api/embed")
            with self.assertRaises(ProviderError):
                provider.embed(["one input"], purpose="document", request_id="bad-native")

    def test_local_profile_identity_binds_every_space_field_and_endpoint(self):
        provider, _transport = self.provider()
        profile = provider.profile
        for change in ({"model": "another"}, {"dimensions": 2}, {"pooling": "mean"}, {"normalization": "none"},
                       {"document_template": "D {text}"}, {"query_template": "Q {text}"}, {"chunker": "v2"},
                       {"endpoint": "http://127.0.0.1:11435/v1/embeddings"}, {"context_window_tokens": 2048}):
            self.assertNotEqual(replace(profile, **change).profile_id, profile.profile_id)
        self.assertEqual(replace(profile, endpoint="http://localhost:11434/v1/embeddings").profile_id, profile.profile_id)
        self.assertNotEqual(profile.profile_id, default_bge_m3_profile().profile_id)
        index = LocalCosineIndex()
        receipt = index.upsert([VectorRecord("00000000-0000-0000-0000-000000000001", (1., 0., 0.))],
                               profile=profile, generation_id="g1", request_id="insert")
        changed = replace(profile, model="another")
        self.assertEqual(index.query((1., 0., 0.), profile=changed, generation_id="g1", request_id="q"), ())
        self.assertEqual(index.query((1., 0., 0.), profile=profile, generation_id="g2", request_id="q"), ())
        with self.assertRaisesRegex(ProviderError, "vector_receipt_binding_mismatch"):
            index.confirm(receipt, profile=changed, request_id="confirm")

    def test_only_strict_loopback_urls_are_admitted_without_dns(self):
        for endpoint in ("http://example.invalid:11434/v1/embeddings", "http://10.0.0.1:80/v1/embeddings",
                         "http://0.0.0.0:80/v1/embeddings", "http://127.1:80/v1/embeddings",
                         "http://2130706433:80/v1/embeddings", "http://127.0.0.1/v1/embeddings",
                         "http://user:secret@127.0.0.1:80/v1/embeddings", "http://localhost.evil:80/v1/embeddings",
                         "http://localhost.:80/v1/embeddings", "http://[::ffff:127.0.0.1]:80/v1/embeddings",
                         "http://[::1%25en0]:80/v1/embeddings", "ftp://127.0.0.1:80/v1/embeddings",
                         "http://127.0.0.1:80/v1/embeddings?token=secret", "http://127.0.0.1:80/v1/embeddings#x",
                         "http://127.0.0.1:80/v1/%65mbeddings", "http://127.0.0.1:80/v1/embeddings\n"):
            with self.subTest(endpoint=endpoint), patch("socket.getaddrinfo", side_effect=AssertionError("DNS")):
                with self.assertRaisesRegex(ProviderError, "embedding_invalid_local_endpoint"):
                    loopback_endpoint(endpoint)
        self.assertEqual(loopback_endpoint("http://[::1]:8080/v1/embeddings"), "http://[::1]:8080/v1/embeddings")

    def test_default_transport_uses_direct_connection_and_never_follows_redirect(self):
        connection = Mock()
        connection.getresponse.return_value = SimpleNamespace(status=307)
        with patch.dict("os.environ", {"HTTP_PROXY": "http://remote.invalid:3128", "ALL_PROXY": "http://remote.invalid:3128"}), \
                patch("http.client.HTTPConnection", return_value=connection) as constructor, \
                patch("urllib.request.urlopen", side_effect=AssertionError("proxy-aware opener")):
            result = LoopbackTransport().request("POST", "http://localhost:11434/v1/embeddings",
                                                headers={}, body=b"{}", timeout=5)
        constructor.assert_called_once_with("127.0.0.1", 11434, timeout=5)
        connection.request.assert_called_once_with("POST", "/v1/embeddings", body=b"{}", headers={})
        connection.close.assert_called_once_with()
        self.assertEqual(result, HTTPResponse(307, b""))


class LocalProviderRuntimeTests(unittest.TestCase):
    @staticmethod
    def native_text(response=None):
        # Match Ollama's concrete fields. An unrestricted Mock invents a
        # client/account_key on access and cannot establish route identity.
        return SimpleNamespace(model="synthetic-text", base_url="http://127.0.0.1:11434",
                               summarize=Mock(return_value=response) if response is not None else
                               Mock(side_effect=AssertionError("unexpected synthetic text call")))

    def roles(self, profile=PROFILE, transport=None, *, native=None, **kwargs):
        native = native if native is not None else self.native_text()
        with patch("core.semantic.runtime.create_provider", return_value=native):
            return ProviderRoles.from_profile({"ai_provider": "ollama"}, profile,
                                              local_transport=transport, **kwargs)

    def test_local_roles_need_no_cloudflare_fields_keychain_or_usage_store(self):
        transport = FakeTransport(reply)
        with patch("core.semantic.runtime.load_key", side_effect=AssertionError("Keychain")), \
                patch("core.semantic.runtime.ProviderUsageStore", side_effect=AssertionError("CF budget")), \
                patch("core.semantic.runtime.CloudflareClient", side_effect=AssertionError("CF client")):
            self.assertEqual(validate_profile(PROFILE), PROFILE)
            native = self.native_text('{"answers":{"q":{"type":"noul","noul":0.7}}}')
            roles = self.roles(transport=transport, native=native)
            status = roles.preflight()
            self.assertTrue(status["ready"])
            self.assertEqual(status["budgets"], [])
            self.assertEqual(set(status["admission"]), {"text", "decision", "embedding"})
            self.assertIs(roles.text.scheduler, roles.scheduler)
            self.assertIs(roles.embedding.scheduler, roles.scheduler)
            self.assertEqual(roles.embedding.embed(["隐藏"], purpose="query", request_id="q").vectors, ((0., 1., 0.),))
            before = roles.scheduler.status()
            answer = roles.decision.decide(DecisionRequest("synthetic state", (DecisionQuestion("q", "noul", "Relevant?"),)), request_id="d")
            self.assertEqual(answer.answers["q"].noul, .7)
            self.assertEqual(answer.route.provider, "ollama")
            native.summarize.assert_called_once()
            after = roles.scheduler.status()
            for role in ("text", "decision"):
                self.assertEqual(after[role]["dispatched"], before[role]["dispatched"] + 1)

    def test_each_selected_cloudflare_role_still_requires_account_and_budget(self):
        for role in ("embedding", "decision", "vector_index"):
            profile = deepcopy(PROFILE)
            profile[role] = "cloudflare"
            if role == "embedding":
                profile.pop("local_embedding")
            if role == "vector_index":
                profile["vectorize_index"] = "synthetic-index"
            with self.assertRaisesRegex(SemanticStoreError, "semantic_provider_profile_invalid"):
                validate_profile(profile)
            profile.update(account_id="a" * 32, secret_name="synthetic-cf-key")
            with tempfile.TemporaryDirectory() as directory, patch("core.semantic.runtime.load_key", return_value="synthetic-token") as key:
                roles = self.roles(profile, FakeTransport(reply), usage_path=str(Path(directory) / "usage.sqlite"))
                key.assert_called_once_with("synthetic-cf-key")
                client = getattr(roles, "index" if role == "vector_index" else role).client
                expected_key = client.vectorize_account_key if role == "vector_index" else client.account_key
                with patch.object(client.usage_store, "budget_status", wraps=client.usage_store.budget_status) as budget:
                    status = roles.preflight()
                    budget.assert_called_once_with(expected_key)
                self.assertFalse(status["ready"])
                self.assertEqual(len(status["budgets"]), 1)

    def test_invalid_profiles_fail_content_free_before_any_io(self):
        invalid = [None, {**PROFILE, "embedding": []}, {**PROFILE, "decision": {}},
                   {**PROFILE, "local_embedding": {**LOCAL, "normalization": []}},
                   {**PROFILE, "local_embedding": {**LOCAL, "api": ["openai"]}},
                   {**PROFILE, "decision": "cloudflare", "account_id": ["a"], "secret_name": "synthetic"}]
        with patch("socket.socket", side_effect=AssertionError("network")), \
                patch("core.semantic.runtime.load_key", side_effect=AssertionError("Keychain")):
            for profile in invalid:
                with self.assertRaisesRegex(SemanticStoreError, "semantic_provider_profile_invalid"):
                    validate_profile(profile)

    def test_existing_cloudflare_profile_has_unchanged_embedding_identity(self):
        profile = {"schema": PROFILE_SCHEMA, "decision": "cloudflare", "embedding": "cloudflare",
                   "vector_index": "cloudflare", "account_id": "b" * 32,
                   "secret_name": "synthetic-key", "vectorize_index": "synthetic-index"}
        with tempfile.TemporaryDirectory() as directory, patch("core.semantic.runtime.load_key", return_value="synthetic-token"):
            roles = self.roles(profile, usage_path=str(Path(directory) / "usage.sqlite"))
            self.assertIsInstance(roles.embedding.provider, CloudflareEmbeddingProvider)
            self.assertEqual(roles.embedding.profile, default_bge_m3_profile())
            self.assertEqual(roles.embedding.route, roles.embedding.provider.route)
            self.assertIs(roles.embedding.scheduler, roles.scheduler)
            self.assertEqual(len(roles.preflight()["budgets"]), 2)

    def test_real_local_adapter_and_index_persist_rehydrate_and_query_dense_evidence(self):
        transport = FakeTransport(reply)
        with tempfile.TemporaryDirectory() as directory, \
                patch("core.semantic.runtime.load_key", side_effect=AssertionError("Keychain")), \
                patch("core.semantic.runtime.ProviderUsageStore", side_effect=AssertionError("CF budget")):
            store = SemanticStore(Path(directory).resolve() / "semantic.sqlite", "local-generation")
            capture(store, "batch", ["普通聊天", "隐藏的中文证据"])
            roles = self.roles(transport=transport)
            retriever = HybridRetriever(store, roles.embedding, roles.index)
            self.assertEqual(retriever.index_pending()["indexed"], 2)
            before = len(transport.calls)
            reopened = HybridRetriever(store, roles.embedding, LocalCosineIndex())
            self.assertEqual(reopened.index_pending()["embedded"], 0)
            self.assertEqual(len(transport.calls), before)
            result = reopened.search("synonym", display_limit=1)
            self.assertEqual(result.hits[0].text, "隐藏的中文证据")
            self.assertEqual(result.hits[0].channels, ("dense",))
            with closing(store.connect()) as connection:
                stored = json.loads(connection.execute("SELECT profile_json FROM index_profiles").fetchone()[0])
                self.assertEqual(stored["endpoint"], LOCAL["endpoint"])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM embedding_records").fetchone()[0], 2)

    def test_unknown_local_request_is_durable_and_not_resent_by_retriever(self):
        def timeout(_call):
            raise TimeoutError("synthetic")
        transport = FakeTransport(timeout)
        with tempfile.TemporaryDirectory() as directory:
            store = SemanticStore(Path(directory).resolve() / "semantic.sqlite", "local-generation")
            capture(store, "batch", ["保留证据"])
            roles = self.roles(transport=transport)
            retriever = HybridRetriever(store, roles.embedding, roles.index)
            with self.assertRaisesRegex(ProviderError, "provider_transport_unknown"):
                retriever.index_pending()
            with self.assertRaisesRegex(Exception, "search_job_busy_or_unknown"):
                HybridRetriever(store, roles.embedding, LocalCosineIndex()).index_pending()
            self.assertEqual(len(transport.calls), 1)
            with closing(store.connect()) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM embedding_records").fetchone()[0], 0)
