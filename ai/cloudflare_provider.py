"""Strict Cloudflare REST boundaries with injected transport and budget evidence."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Mapping, Protocol
import urllib.error
import urllib.request

from core.provider_usage import ProviderUsageError, ProviderUsageStore
from .semantic_provider import (
    DecisionResult, EmbeddingProfile, EmbeddingResult, ProviderError,
    ProviderRoute, json_bytes, parse_decision_answers, strict_json_loads, validate_vector,
)

CLEF_MODEL = "@cf/cloudflare/clef-flash"
BGE_MODEL = "@cf/baai/bge-m3"
# Integer micro-neurons per input token, from the 2026-10-13 official table.
CLEF_MICRO_NEURONS_PER_TOKEN = 3455
BGE_MICRO_NEURONS_PER_TOKEN = 1075


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes


class HTTPTransport(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str],
                body: bytes | None, timeout: float) -> HTTPResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """No redirects, logging or retries; callers erase all raw error details."""

    def request(self, method, url, *, headers, body, timeout):
        opener = urllib.request.build_opener(_NoRedirect())
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = response.read(32 * 1024 * 1024 + 1)
                if len(payload) > 32 * 1024 * 1024:
                    raise ProviderError("provider_response_too_large", outcome_unknown=True)
                return HTTPResponse(response.status, payload)
        except urllib.error.HTTPError as exc:
            # Do not retain the provider error body or HTTPError's URL.
            code = exc.code
            exc.close()
            return HTTPResponse(code, b"")


class CloudflareClient:
    """Explicit account and credentials; construction has no external effects."""

    def __init__(self, account_id, api_token, usage_store: ProviderUsageStore,
                 *, transport=None, timeout=45):
        if not isinstance(account_id, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", account_id):
            raise ProviderError("provider_invalid_account")
        if not isinstance(api_token, str) or not api_token.strip() or any(char.isspace() for char in api_token):
            raise ProviderError("provider_missing_credentials")
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ProviderError("provider_invalid_timeout")
        self._account_id = account_id.lower()
        self._api_token = api_token
        self.usage_store = usage_store
        self.transport = transport or UrllibTransport()
        self.timeout = timeout

    @property
    def account_key(self):
        return "cf-ai:" + hashlib.sha256(self._account_id.encode("ascii")).hexdigest()

    @property
    def vectorize_account_key(self):
        return "cf-vectorize:" + hashlib.sha256(self._account_id.encode("ascii")).hexdigest()

    def reserve(self, *, path, method, body, request_id, route, upper_bound_units,
                generation_id="", service="ai"):
        key = self.account_key if service == "ai" else self.vectorize_account_key
        digest = hashlib.sha256(method.encode() + b"\0" + path.encode() + b"\0" + (body or b"")).hexdigest()
        try:
            return self.usage_store.reserve(key, request_id=request_id, route=route,
                                           upper_bound_units=upper_bound_units,
                                           request_fingerprint=digest, generation_id=generation_id)
        except ProviderUsageError as exc:
            raise ProviderError(exc.code) from None

    def request(self, path, *, method="POST", payload=None, body=None,
                content_type="application/json", request_id, route,
                upper_bound_units, generation_id="", service="ai",
                usage_units=None):
        if not isinstance(path, str) or not path.startswith(("/ai/run/", "/vectorize/v2/indexes/")) or ".." in path:
            raise ProviderError("provider_invalid_route")
        if body is None and payload is not None:
            body = json_bytes(payload)
        self.reserve(path=path, method=method, body=body, request_id=request_id,
                     route=route, upper_bound_units=upper_bound_units,
                     generation_id=generation_id, service=service)
        try:
            self.usage_store.mark_sent(request_id)
        except ProviderUsageError as exc:
            raise ProviderError(exc.code) from None
        try:
            response = self.transport.request(
                method, f"https://api.cloudflare.com/client/v4/accounts/{self._account_id}{path}",
                headers={"Authorization": f"Bearer {self._api_token}", "Content-Type": content_type},
                body=body, timeout=self.timeout,
            )
            if not isinstance(response, HTTPResponse) or type(response.status) is not int:
                raise ProviderError("provider_invalid_http_response", outcome_unknown=True)
            if not 200 <= response.status < 300:
                code = "provider_rate_limited" if response.status == 429 else "provider_http_error"
                raise ProviderError(code, retryable=response.status in {408, 425, 429} or response.status >= 500,
                                    outcome_unknown=True)
            envelope = strict_json_loads(response.body, outcome_unknown=True)
            if not isinstance(envelope, dict) or envelope.get("success") is not True:
                raise ProviderError("provider_reported_failure", outcome_unknown=True)
            if envelope.get("errors") not in (None, []) or not isinstance(envelope.get("result"), (dict, list)):
                raise ProviderError("provider_invalid_result", outcome_unknown=True)
            result = envelope["result"]
            actual = usage_units(result) if usage_units else upper_bound_units
        except Exception as exc:
            try:
                self.usage_store.mark_unknown(request_id)
            except ProviderUsageError:
                raise ProviderError("usage_receipt_unavailable", outcome_unknown=True) from None
            if isinstance(exc, ProviderError):
                raise exc from None
            raise ProviderError("provider_transport_unknown", retryable=True, outcome_unknown=True) from None
        try:
            self.usage_store.settle(request_id, actual_units=actual,
                                    receipt_id="cf:" + hashlib.sha256(request_id.encode()).hexdigest())
        except ProviderUsageError as exc:
            raise ProviderError(exc.code, outcome_unknown=True) from None
        return result


class CloudflareDecisionProvider:
    route = ProviderRoute("cloudflare", CLEF_MODEL, "decision")

    def __init__(self, client):
        self.client = client

    def decide(self, request, *, request_id, generation_id=""):
        payload = {"model": "clef-flash", **request.as_payload()}
        # Clef's server may truncate state. A UTF-8 byte upper bound plus
        # generous rendering/special-token headroom rejects before that path.
        if len(json_bytes(payload)) + 4096 > 24576:
            raise ProviderError("decision_input_too_large")

        def observed_usage(result):
            usage = result.get("usage") if isinstance(result, dict) else None
            if not isinstance(usage, dict) or type(usage.get("input_tokens")) is not int or usage["input_tokens"] < 0:
                raise ProviderError("decision_invalid_usage", outcome_unknown=True)
            if type(usage.get("output_tokens")) is not int or usage["output_tokens"] != 0:
                raise ProviderError("decision_invalid_usage", outcome_unknown=True)
            return usage["input_tokens"] * CLEF_MICRO_NEURONS_PER_TOKEN

        result = self.client.request(
            "/ai/run/" + CLEF_MODEL, payload=payload, request_id=request_id,
            route=self.route.key, upper_bound_units=24576 * CLEF_MICRO_NEURONS_PER_TOKEN,
            generation_id=generation_id, usage_units=observed_usage,
        )
        if not isinstance(result, dict) or result.get("model") != "clef-flash":
            raise ProviderError("decision_model_mismatch")
        answers = parse_decision_answers(result.get("answers"), request, prepared_questions=payload["questions"])
        return DecisionResult(answers, self.route, result["usage"]["input_tokens"])


def default_bge_m3_profile():
    return EmbeddingProfile("cloudflare", BGE_MODEL, 1024, pooling="cls")


class CloudflareEmbeddingProvider:
    """BGE-M3 dense embeddings; bounded length buckets, no input truncation."""

    def __init__(self, client, profile=None, *, max_batch_size=32,
                 max_input_bytes=8000, max_padded_bytes=32000):
        self.client = client
        self.profile = profile or default_bge_m3_profile()
        if (self.profile.provider, self.profile.model, self.profile.dimensions, self.profile.pooling) != (
                "cloudflare", BGE_MODEL, 1024, "cls"):
            raise ProviderError("embedding_unsupported_profile")
        if (type(max_batch_size) is not int or not 1 <= max_batch_size <= 100
                or type(max_input_bytes) is not int or not 1 <= max_input_bytes <= 8000
                or type(max_padded_bytes) is not int or not max_input_bytes <= max_padded_bytes <= 50000):
            raise ProviderError("embedding_invalid_limits")
        self.max_batch_size = max_batch_size
        self.max_input_bytes = max_input_bytes
        self.max_padded_bytes = max_padded_bytes
        self.route = ProviderRoute("cloudflare", BGE_MODEL, "embedding")

    def _batches(self, texts, purpose, request_id):
        if not isinstance(texts, (list, tuple)) or not texts or not isinstance(request_id, str) or not request_id:
            raise ProviderError("embedding_invalid_input")
        rows = [(i, self.profile.prepare(text, purpose)) for i, text in enumerate(texts)]
        # json_bytes also rejects invalid Unicode before size calculation.
        json_bytes([text for _i, text in rows])
        if any(len(text.encode("utf-8")) > self.max_input_bytes for _i, text in rows):
            raise ProviderError("embedding_input_too_large")
        rows.sort(key=lambda row: (len(row[1].encode("utf-8")), row[0]))
        groups = []
        group = []
        for row in rows:
            padded = (len(row[1].encode("utf-8")) + 64) * (len(group) + 1)
            if group and (len(group) == self.max_batch_size or padded > self.max_padded_bytes):
                groups.append(group)
                group = []
            group.append(row)
        if group:
            groups.append(group)
        result = []
        for number, group in enumerate(groups):
            body = json_bytes({"text": [text for _i, text in group], "truncate_inputs": False})
            # Byte count bounds tokenizer input; padding and 64 special tokens
            # per row intentionally over-reserve. BGE has no usage in response.
            bound = (max(len(text.encode("utf-8")) for _i, text in group) + 64) * len(group)
            identifier = "emb:" + hashlib.sha256((request_id + ":" + str(number)).encode()).hexdigest()
            result.append((identifier, group, body, bound * BGE_MICRO_NEURONS_PER_TOKEN))
        return result

    def embed(self, texts, *, purpose, request_id, generation_id=""):
        batches = self._batches(texts, purpose, request_id)
        path = "/ai/run/" + BGE_MODEL
        reserved = []
        try:
            # Admit the entire operation before any network call.
            for identifier, _group, body, bound in batches:
                self.client.reserve(path=path, method="POST", body=body, request_id=identifier,
                                    route=self.route.key, upper_bound_units=bound, generation_id=generation_id)
                reserved.append(identifier)
        except ProviderError:
            for identifier in reserved:
                self.client.usage_store.cancel(identifier)
            raise
        output = [None] * len(texts)
        sent = set()
        try:
            for identifier, group, body, bound in batches:
                sent.add(identifier)
                result = self.client.request(path, body=body, request_id=identifier,
                                             route=self.route.key, upper_bound_units=bound, generation_id=generation_id)
                shape = result.get("shape") if isinstance(result, dict) else None
                if (not isinstance(shape, list) or any(type(size) is not int for size in shape)
                        or shape != [len(group), self.profile.dimensions]):
                    raise ProviderError("embedding_shape_mismatch")
                if result.get("pooling", "cls") != self.profile.pooling:
                    raise ProviderError("embedding_pooling_mismatch")
                data = result.get("data")
                if not isinstance(data, list) or len(data) != len(group):
                    raise ProviderError("embedding_count_mismatch")
                for (original, _text), vector in zip(group, data):
                    output[original] = validate_vector(vector, self.profile, normalize=True)
        finally:
            for identifier in reserved:
                if identifier not in sent:
                    self.client.usage_store.cancel(identifier)
        return EmbeddingResult(tuple(output), self.profile.profile_id, self.route)
