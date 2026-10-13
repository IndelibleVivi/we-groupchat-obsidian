"""Bounded in-process provider admission; canonical jobs own durable outcomes.

Rate limits are explicit operator ceilings, never inferred vendor allowances.
One failed phase stops admitting new work and drains calls already dispatched.
No executor thread owns a database transaction or a projection/index file lock.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import threading
import time
import weakref

from ai.semantic_provider import ProviderError, TextDecisionProvider


DEFAULT_LIMITS = {
    "text": {"max_inflight": 8, "requests_per_minute": None, "tokens_per_minute": None},
    "decision": {"max_inflight": 4, "requests_per_minute": None, "tokens_per_minute": None},
    "embedding": {"max_inflight": 4, "requests_per_minute": None, "tokens_per_minute": None},
}


def validate_limits(value=None):
    """Normalize a partial, explicit configuration; reject misspelled limits."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - DEFAULT_LIMITS.keys():
        raise ValueError("semantic_provider_limits_invalid")
    result = {}
    for role, default in DEFAULT_LIMITS.items():
        incoming = value.get(role, {})
        if not isinstance(incoming, dict) or set(incoming) - default.keys():
            raise ValueError("semantic_provider_limits_invalid")
        row = {**default, **incoming}
        for key, maximum in (("max_inflight", 64), ("requests_per_minute", 1_000_000),
                             ("tokens_per_minute", 1_000_000_000)):
            if row[key] is None and key != "max_inflight":
                continue
            if type(row[key]) is not int or not 1 <= row[key] <= maximum:
                raise ValueError("semantic_provider_limits_invalid")
        result[role] = row
    return result


_UNSENT = object()
_PHASE = ContextVar("semantic_provider_phase", default=None)
_LEASES = ContextVar("semantic_provider_leases", default=())


class ProviderNotDispatched(ProviderError):
    """Only scheduler-issued evidence can return a claimed job to pending."""

    def __init__(self, code, request_id, *, _proof):
        if _proof is not _UNSENT:
            raise ValueError("provider_admission_proof_required")
        super().__init__(code, outcome_unknown=False)
        self.request_id, self._proof = request_id, _proof


def not_dispatched(exc, request_id=None):
    return (type(exc) is ProviderNotDispatched and exc._proof is _UNSENT
            and (request_id is None or exc.request_id == request_id))


@contextmanager
def maintain_claim(callback):
    """Renew a canonical lease while admission waits, without holding its DB."""
    token = _LEASES.set((*_LEASES.get(), callback))
    try:
        yield
    finally:
        _LEASES.reset(token)


class _Phase:
    def __init__(self, gates):
        self.gates = frozenset(id(gate) for gate in gates)
        self.stopped = threading.Event()
        self.error = None
        self.lock = threading.Lock()

    def stop(self, error):
        with self.lock:
            if self.error is None or not_dispatched(self.error):
                self.error = error
            self.stopped.set()


@dataclass
class _Gate:
    limits: dict

    def __post_init__(self):
        self.active = self.peak = self.dispatched = 0
        self.reservations = deque()
        self.waiters = deque()


_SHARED = weakref.WeakValueDictionary()
# Keep rate reservations across short-lived app timer runtimes. The keys and
# counters are process-local; canonical jobs/usage remain the durable owners.
_SHARED_GATES = {}
_SHARED_LOCK = threading.Lock()
_CONDITION = threading.Condition(threading.RLock())
_PHASES = set()


class ProviderScheduler:
    def __init__(self, limits=None, *, clock=time.monotonic, window_seconds=60):
        self.limits = validate_limits(limits)
        self.clock, self.window = clock, window_seconds
        self.condition = _CONDITION
        self.gates = {role: _Gate(row) for role, row in self.limits.items()}

    @classmethod
    def shared(cls, limits, identity):
        normalized = validate_limits(limits)
        key = json.dumps([identity, normalized], sort_keys=True, separators=(",", ":"))
        with _SHARED_LOCK:
            scheduler = _SHARED.get(key)
            if scheduler is None:
                scheduler = cls(normalized)
                for role, gate in scheduler.gates.items():
                    gate_key = json.dumps([role, identity[role]], sort_keys=True)
                    shared = _SHARED_GATES.get(gate_key)
                    if shared is None:
                        _SHARED_GATES[gate_key] = gate
                    else:
                        # Overlapping old/new runtime settings cannot multiply
                        # one account's admission. Keep the stricter live cap.
                        with _CONDITION:
                            shared.limits = {field: min(v for v in (shared.limits[field], gate.limits[field]) if v is not None)
                                             if shared.limits[field] is not None or gate.limits[field] is not None else None
                                             for field in gate.limits}
                        scheduler.gates[role] = shared
                _SHARED[key] = scheduler
            return scheduler

    def status(self):
        with self.condition:
            return {role: {**gate.limits, "inflight": gate.active, "peak_inflight": gate.peak,
                           "dispatched": gate.dispatched,
                           "unset": [key for key in ("requests_per_minute", "tokens_per_minute")
                                     if gate.limits[key] is None]}
                    for role, gate in self.gates.items()}

    @contextmanager
    def phase(self):
        current = _PHASE.get()
        if current is not None and current[0] is self:
            yield current[1]
            return
        phase = _Phase(self.gates.values())
        with self.condition:
            _PHASES.add(phase)
        token = _PHASE.set((self, phase))
        try:
            yield phase
        finally:
            _PHASE.reset(token)
            with self.condition:
                _PHASES.discard(phase)

    def _stop(self, role, exc):
        with self.condition:
            for phase in _PHASES:
                if id(self.gates[role]) in phase.gates:
                    phase.stop(exc)
            self.condition.notify_all()

    def call(self, role, tokens, request_id, callback):
        if role not in self.gates or type(tokens) is not int or tokens < 0:
            raise ValueError("provider_admission_invalid")
        context = _PHASE.get()
        if context is None or context[0] is not self:
            with self.phase():
                return self.call(role, tokens, request_id, callback)
        phase = context[1] if context is not None and context[0] is self else None
        gate, ticket = self.gates[role], object()
        admitted = False
        renewed = self.clock()
        with self.condition:
            gate.waiters.append(ticket)
        try:
            while not admitted:
                if self.clock() - renewed >= 30:
                    for renew in _LEASES.get():
                        renew()
                    renewed = self.clock()
                with self.condition:
                    if phase is not None and phase.stopped.is_set():
                        raise ProviderNotDispatched("provider_admission_stopped", request_id, _proof=_UNSENT)
                    ceiling = gate.limits["tokens_per_minute"]
                    if ceiling is not None and tokens > ceiling:
                        raise ProviderNotDispatched("provider_token_admission_exceeded", request_id, _proof=_UNSENT)
                    now = self.clock()
                    while gate.reservations and gate.reservations[0][0] <= now - self.window:
                        gate.reservations.popleft()
                    rpm = gate.limits["requests_per_minute"]
                    available = (gate.waiters[0] is ticket and gate.active < gate.limits["max_inflight"]
                        and (rpm is None or len(gate.reservations) < rpm)
                        and (ceiling is None or sum(n for _, n in gate.reservations) + tokens <= ceiling))
                    if available:
                        gate.waiters.popleft()
                        gate.reservations.append((now, tokens))
                        gate.active += 1
                        gate.peak = max(gate.peak, gate.active)
                        gate.dispatched += 1
                        admitted = True
                    else:
                        delay = (max(.001, gate.reservations[0][0] + self.window - now)
                                 if gate.reservations else 1)
                        self.condition.wait(min(1, delay))
            # This boundary is the only transition from not-dispatched to an
            # actual provider invocation. Drain it even if a sibling now fails.
            return callback()
        except Exception as exc:
            self._stop(role, exc)
            raise
        finally:
            with self.condition:
                if admitted:
                    gate.active -= 1
                elif ticket in gate.waiters:
                    gate.waiters.remove(ticket)
                self.condition.notify_all()

    def map(self, role, values, operation):
        """Ordered results, bounded submission and drain-on-error, without retry."""
        values = iter(values)
        results, futures, exhausted = {}, {}, False
        with self.phase() as phase:
            def run(index, value):
                token = _PHASE.set((self, phase))
                try:
                    if phase.stopped.is_set():
                        raise ProviderNotDispatched("provider_admission_stopped", "", _proof=_UNSENT)
                    return operation(value)
                except Exception as exc:
                    phase.stop(exc)
                    with self.condition:
                        self.condition.notify_all()
                    raise
                finally:
                    _PHASE.reset(token)

            count = 0
            with ThreadPoolExecutor(max_workers=self.limits[role]["max_inflight"],
                                    thread_name_prefix="semantic-" + role) as executor:
                while futures or not exhausted:
                    while not exhausted and not phase.stopped.is_set() and len(futures) < self.limits[role]["max_inflight"]:
                        try:
                            value = next(values)
                        except StopIteration:
                            exhausted = True
                            break
                        futures[executor.submit(run, count, value)] = count
                        count += 1
                    if not futures:
                        break
                    finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in finished:
                        index = futures.pop(future)
                        try:
                            results[index] = future.result()
                        except Exception as exc:
                            phase.stop(exc)
                    if phase.stopped.is_set():
                        exhausted = True
            if phase.error is not None:
                raise phase.error
        return [results[index] for index in range(count)]


class _ScheduledProvider:
    def __init__(self, provider, scheduler, *, output_tokens=16384):
        self.provider, self.scheduler, self.output_tokens = provider, scheduler, output_tokens

    def __getattr__(self, key):
        return getattr(self.provider, key)


class ScheduledText(_ScheduledProvider):
    def generate(self, prompt, *, request_id, generation_id=""):
        bound = len(prompt.encode("utf-8")) + self.output_tokens + 256
        return self.scheduler.call("text", bound, request_id,
            lambda: self.provider.generate(prompt, request_id=request_id, generation_id=generation_id))


class ScheduledDecision(_ScheduledProvider):
    def decide(self, request, *, request_id, generation_id=""):
        # Clef consumes inputs only; the explicit text fallback also reserves
        # its actual generated-text call on the Text gate.
        bound = len(json.dumps(request.as_payload(), ensure_ascii=False).encode("utf-8")) + 4096
        if isinstance(self.provider, TextDecisionProvider):
            bound += self.provider.text_provider.output_tokens
        return self.scheduler.call("decision", bound, request_id,
            lambda: self.provider.decide(request, request_id=request_id, generation_id=generation_id))


class ScheduledEmbedding(_ScheduledProvider):
    def embed(self, texts, *, purpose, request_id, generation_id=""):
        prepared = [self.profile.prepare(text, purpose) for text in texts]
        bound = sum(len(text.encode("utf-8")) + 64 for text in prepared)
        return self.scheduler.call("embedding", bound, request_id,
            lambda: self.provider.embed(texts, purpose=purpose, request_id=request_id, generation_id=generation_id))


def scheduled_providers(text, decision, embedding, *, limits=None, output_tokens=16384, scheduler=None):
    def raw(provider):
        return provider.provider if isinstance(provider, _ScheduledProvider) else provider
    text, decision, embedding = raw(text), raw(decision), raw(embedding)
    def identity(provider):
        if isinstance(provider, TextDecisionProvider):
            return [provider.route.key, identity(raw(provider.text_provider))]
        underlying = getattr(provider, "provider", provider)
        client = getattr(provider, "client", None) or getattr(underlying, "client", None)
        account = getattr(client, "account_key", None)
        credential = getattr(client, "api_key", None) or getattr(client, "_api_token", None)
        # This is only an in-memory equality binding. No secret or endpoint is
        # emitted by status, persisted, sent to another service or logged.
        credential = hashlib.sha256(credential.encode()).hexdigest() if isinstance(credential, str) else None
        endpoint = getattr(client, "base_url", None) or getattr(underlying, "base_url", "")
        return [provider.route.key, account, credential, str(endpoint),
                getattr(getattr(provider, "profile", None), "profile_id", None)]
    identity = {"text": identity(text), "decision": identity(decision), "embedding": identity(embedding)}
    scheduler = scheduler or ProviderScheduler.shared(limits, identity)
    writer = ScheduledText(text, scheduler, output_tokens=output_tokens)
    if isinstance(decision, TextDecisionProvider):
        decision = TextDecisionProvider(writer)
    return (writer, ScheduledDecision(decision, scheduler), ScheduledEmbedding(embedding, scheduler), scheduler)
