"""Explicit loopback embeddings, with no discovery, proxy, redirect or fallback."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
from urllib.parse import urlsplit, urlunsplit

from .cloudflare_provider import HTTPResponse
from .semantic_provider import (
    EmbeddingProfile, EmbeddingResult, ProviderError, ProviderRoute,
    json_bytes, strict_json_loads, validate_vector,
)


def loopback_endpoint(value):
    """Canonicalize an explicit numeric loopback URL without DNS or IO.

    localhost is pinned to IPv4 loopback rather than resolved via DNS. Ports
    are explicit; URL credentials, escapes, query strings and fragments are
    never accepted. The returned value is private configuration, not a log.
    """
    try:
        if (not isinstance(value, str) or not value or len(value) > 2048
                or any(character.isspace() or ord(character) < 32 for character in value)
                or any(character in value for character in ("\\", "%", "?", "#"))):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None
                or parsed.password is not None or parsed.port is None
                or not 1 <= parsed.port <= 65535
                or parsed.path not in {"/v1/embeddings", "/api/embed"}):
            raise ValueError
        host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
        address = ipaddress.ip_address(host)
        if not address.is_loopback or (address.version == 6 and address != ipaddress.IPv6Address("::1")):
            raise ValueError
        authority = f"[{address}]:{parsed.port}" if address.version == 6 else f"{address}:{parsed.port}"
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    except (ValueError, TypeError):
        raise ProviderError("embedding_invalid_local_endpoint") from None


@dataclass(frozen=True)
class LocalEmbeddingProfile(EmbeddingProfile):
    endpoint: str = ""
    api: str = "openai"
    context_window_tokens: int = 0
    max_input_bytes: int = 0

    def __post_init__(self):
        super().__post_init__()
        if self.provider != "local" or self.api not in ("openai", "ollama"):
            raise ProviderError("embedding_invalid_local_profile")
        endpoint = loopback_endpoint(self.endpoint)
        expected_path = "/v1/embeddings" if self.api == "openai" else "/api/embed"
        if urlsplit(endpoint).path != expected_path:
            raise ProviderError("embedding_invalid_local_endpoint")
        # The operator binds the deployed model's actual context size. UTF-8
        # bytes conservatively bound byte/subword input, with special-token
        # headroom; this is not model discovery or a guessed model default.
        if (type(self.context_window_tokens) is not int or not 65 <= self.context_window_tokens <= 1_000_000
                or type(self.max_input_bytes) is not int
                or not 1 <= self.max_input_bytes <= self.context_window_tokens - 64):
            raise ProviderError("embedding_invalid_limits")
        object.__setattr__(self, "endpoint", endpoint)


class LoopbackTransport:
    """HTTPTransport using direct sockets: no environment proxy or redirects."""

    def request(self, method, url, *, headers, body, timeout):
        parsed = urlsplit(loopback_endpoint(url))
        if method != "POST":
            raise ProviderError("embedding_invalid_local_request")
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
        try:
            connection.request(method, parsed.path, body=body, headers=dict(headers))
            response = connection.getresponse()
            # http.client exposes 3xx as a response and never follows it.
            if not 200 <= response.status < 300:
                return HTTPResponse(response.status, b"")
            payload = response.read(32 * 1024 * 1024 + 1)
            if len(payload) > 32 * 1024 * 1024:
                raise ProviderError("provider_response_too_large", outcome_unknown=True)
            return HTTPResponse(response.status, payload)
        finally:
            connection.close()


class LocalEmbeddingProvider:
    """A complete explicit local route. Only the injected transport performs IO.

    OpenAI-compatible servers return indexed rows. Ollama's explicitly selected
    native API returns positional rows and can enforce truncate:false. Neither
    route switches API, endpoint or model when a call fails.
    """

    def __init__(self, profile, *, transport=None, timeout=45, max_batch_size=32,
                 max_batch_bytes=32000):
        if not isinstance(profile, LocalEmbeddingProfile):
            raise ProviderError("embedding_invalid_local_profile")
        if (type(timeout) not in (int, float) or not 0 < timeout <= 300
                or type(max_batch_size) is not int or not 1 <= max_batch_size <= 128
                or type(max_batch_bytes) is not int
                or not profile.max_input_bytes <= max_batch_bytes <= 8_000_000):
            raise ProviderError("embedding_invalid_limits")
        self.profile = profile
        self.route = ProviderRoute("local", profile.model, "embedding")
        self.transport = transport if transport is not None else LoopbackTransport()
        self.timeout, self.max_batch_size, self.max_batch_bytes = timeout, max_batch_size, max_batch_bytes

    @classmethod
    def from_config(cls, config, *, transport=None):
        """Validate local_embedding without probing a server or loading secrets."""
        required = {"endpoint", "model", "dimensions", "context_window_tokens"}
        optional = {"api", "pooling", "normalization", "document_template", "query_template", "chunker",
                    "metric", "max_input_bytes", "max_batch_size", "max_batch_bytes", "timeout"}
        if not isinstance(config, dict) or not required <= config.keys() or config.keys() - required - optional:
            raise ProviderError("embedding_invalid_local_profile")
        context = config["context_window_tokens"]
        if type(context) is not int:
            raise ProviderError("embedding_invalid_limits")
        fields = {key: value for key, value in config.items()
                  if key not in {"max_batch_size", "max_batch_bytes", "timeout"}}
        fields.setdefault("max_input_bytes", min(8000, context - 64))
        try:
            profile = LocalEmbeddingProfile(provider="local", **fields)
        except (TypeError, ValueError, OverflowError):
            raise ProviderError("embedding_invalid_local_profile") from None
        return cls(profile, transport=transport,
                   **{key: config[key] for key in ("max_batch_size", "max_batch_bytes", "timeout") if key in config})

    def _batches(self, texts, purpose, request_id):
        if not isinstance(texts, (list, tuple)) or not texts or not isinstance(request_id, str) or not request_id:
            raise ProviderError("embedding_invalid_input")
        rows = [(index, self.profile.prepare(text, purpose)) for index, text in enumerate(texts)]
        json_bytes([text for _index, text in rows])  # Reject invalid Unicode before any request.
        sizes = [len(text.encode("utf-8")) for _index, text in rows]
        if any(size > self.profile.max_input_bytes for size in sizes):
            raise ProviderError("embedding_input_too_large")
        # Length buckets bound padded input as well as count. Never split or
        # crop a source document: canonical callers retain chunk ownership.
        rows.sort(key=lambda row: (sizes[row[0]], row[0]))
        batches, group = [], []
        for row in rows:
            if group and (len(group) >= self.max_batch_size
                          or sizes[row[0]] * (len(group) + 1) > self.max_batch_bytes):
                batches.append(group)
                group = []
            group.append(row)
        if group:
            batches.append(group)
        return batches

    def embed(self, texts, *, purpose, request_id, generation_id=""):
        batches = self._batches(texts, purpose, request_id)
        vectors = [None] * len(texts)
        for group in batches:
            payload = {"model": self.profile.model, "input": [text for _index, text in group]}
            if self.profile.api == "openai":
                payload["encoding_format"] = "float"
            else:
                payload["truncate"] = False
            try:
                response = self.transport.request(
                    "POST", self.profile.endpoint,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    body=json_bytes(payload), timeout=self.timeout,
                )
                if not isinstance(response, HTTPResponse) or type(response.status) is not int:
                    raise ProviderError("provider_invalid_http_response", outcome_unknown=True)
                if 300 <= response.status < 400:
                    raise ProviderError("embedding_redirect_rejected", outcome_unknown=True)
                if not 200 <= response.status < 300:
                    raise ProviderError("provider_rate_limited" if response.status == 429 else "provider_http_error",
                                        retryable=response.status in {408, 425, 429} or response.status >= 500,
                                        outcome_unknown=True)
                result = strict_json_loads(response.body, outcome_unknown=True)
                if not isinstance(result, dict) or result.get("error") is not None:
                    raise ProviderError("embedding_invalid_response")
                if result.get("model") != self.profile.model:
                    raise ProviderError("embedding_model_mismatch")
                if self.profile.api == "openai":
                    data = result.get("data")
                    if result.get("object") != "list" or not isinstance(data, list) or len(data) != len(group):
                        raise ProviderError("embedding_count_mismatch")
                    indexed = {}
                    for row in data:
                        if (not isinstance(row, dict) or row.get("object") != "embedding"
                                or type(row.get("index")) is not int or not 0 <= row["index"] < len(group)
                                or row["index"] in indexed):
                            raise ProviderError("embedding_index_mismatch")
                        indexed[row["index"]] = row.get("embedding")
                    data = [indexed[index] for index in range(len(group))]
                else:
                    data = result.get("embeddings")
                    if not isinstance(data, list) or len(data) != len(group):
                        raise ProviderError("embedding_count_mismatch")
                for (original, _text), vector in zip(group, data):
                    vectors[original] = validate_vector(vector, self.profile, normalize=True)
            except ProviderError:
                raise
            except Exception:
                raise ProviderError("provider_transport_unknown", retryable=True, outcome_unknown=True) from None
        return EmbeddingResult(tuple(vectors), self.profile.profile_id, self.route)
