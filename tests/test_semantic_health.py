"""Synthetic health evidence, including closed failures and zero side effects."""
from contextlib import ExitStack, closing, contextmanager, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from core.monitor_state import MonitorStateStore
from core.provider_usage import ProviderUsageStore
from core.semantic.health import HEALTH_SCHEMA, semantic_health
from core.semantic.jobs import JobStore
from core.semantic.rebuild import ACTIVE_SCHEMA, JOURNAL_SCHEMA, write_json
from core.semantic.store import SemanticStore
from core.semantic.controls import ReaderControls
from core.semantic.delivery import AttentionEvent, ContentBinding, DeliveryService, EvidenceTarget, NotifyResult
from core.semantic.projections import ProjectionPublisher
from tests.test_monitor_state_v3 import _state, _migration, _capture, _vector


PROFILE_SCHEMA = "we-groupchat-obsidian.semantic-providers.v1"
LOCAL_EMBEDDING = {"endpoint": "http://127.0.0.1:11434/v1/embeddings", "model": "synthetic-local",
                   "dimensions": 3, "context_window_tokens": 1024}


class SemanticHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / "runtime"
        self.now = 1800000000.0
        self.generation = "synthetic-generation"
        self.config = {"semantic_runtime_root": str(self.runtime), "db_dir": str(self.root / "protected-source")}
        self.database = self.runtime / "generations" / self.generation / "knowledge.sqlite"
        self.usage = self.root / "provider-usage.sqlite"
        self.checkpoint = self.root / "checkpoint.json"
        self.account = "a" * 32
        self.account_key = "cf-ai:" + hashlib.sha256(self.account.encode()).hexdigest()

    def local_profile(self):
        return {"schema": PROFILE_SCHEMA, "decision": "text", "embedding": "local", "vector_index": "local",
                "local_embedding": dict(LOCAL_EMBEDDING)}

    def generation_fixture(self, *, active=True, activated=True, profile=None):
        self.store = SemanticStore(self.database, self.generation, now_func=lambda: self.now)
        metadata = {"provider_profile": profile if profile is not None else {
            "schema": PROFILE_SCHEMA, "decision": "cloudflare", "embedding": "cloudflare",
            "vector_index": "local", "account_id": self.account, "secret_name": "synthetic-reference"}}
        if activated:
            metadata["activation"] = {"run_id": "run", "activated_at": self.now}
        with self.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json=?", (json.dumps(metadata),))
        self.manifest = {"schema": ACTIVE_SCHEMA, "generation_id": self.generation,
            "database": str(self.database), "embedding_profile_id": "synthetic-embedding",
            "index_generation_id": "synthetic-index", "projection_root": str(self.root / "vault"),
            "projection_receipt_id": "projection", "migration_receipt_ids": ["migration"],
            "config_revision": 1, "acceptance_receipt_id": "accepted", "run_id": "run"}
        if active:
            write_json(self.runtime / "active.json", self.manifest)
        state_store = MonitorStateStore(self.checkpoint)
        old = state_store.initialize_if_absent({"last_checked_ts": 100})
        state = _state(generation=self.generation)
        self.snapshot = state_store.install_v3(old.revision, state, _migration(old, state))
        return self.store

    def budget_fixture(self, *, now=None, account_key=None):
        now = self.now if now is None else now
        usage = ProviderUsageStore(self.usage, clock=lambda: now)
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        usage.confirm_budget(account_key or self.account_key, utc_day=day, daily_limit_units=100,
                             external_usage_units=1, confirmation_id="synthetic-proof", valid_until=now + 100)
        return usage

    def set_profile(self, profile):
        with self.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json=json_set(metadata_json,'$.provider_profile',json(?))",
                         (json.dumps(profile),))

    @contextmanager
    def no_usage_access(self):
        def guarded(original):
            def run(path, *args, **kwargs):
                if self.usage.name in str(path):
                    raise AssertionError("unneeded usage path was inspected")
                return original(path, *args, **kwargs)
            return run
        with ExitStack() as stack:
            for target in ("stat", "lstat", "open"):
                stack.enter_context(patch.object(os, target, side_effect=guarded(getattr(os, target))))
            stack.enter_context(patch("sqlite3.connect", side_effect=guarded(sqlite3.connect)))
            yield

    def report(self, **kwargs):
        return semantic_health(self.config, checkpoint_paths=[self.checkpoint], usage_path=self.usage, now=self.now, **kwargs)

    def bytes_and_modes(self):
        # Existing SQLite shared-memory read marks are lock coordination, not
        # durable state. File presence/mode and all DB/WAL bytes stay unchanged.
        return {str(p.relative_to(self.root)): (None if p.name.endswith("-shm") else p.read_bytes(), p.stat().st_mode)
                for p in self.root.rglob("*") if p.is_file()}

    def delivery_fixture(self, *, status="accepted", event="synthetic-event"):
        publisher = ProjectionPublisher.for_store(self.store, self.root / "vault")
        controls = ReaderControls(publisher.ledger, publisher, search=Mock())
        service = DeliveryService(publisher.ledger, publisher, controls)
        attention = AttentionEvent(event, "private-item", 1, "private-title", "private-body", "brief",
            (ContentBinding("private-item", 1, 0),), (EvidenceTarget("private-item", "private-evidence"),))
        registered = service.register(attention)
        notifier = Mock()
        notifier.send.side_effect = lambda envelope: NotifyResult(status, envelope.receipt_id)
        return service, registered.event_id, notifier

    def send_delivery(self, service, event_id, notifier, *, live=True, ready=True):
        # The actual durable delivery owner crosses a synthetic publication and
        # notifier boundary; health itself must never cross either boundary.
        with patch.object(service.publisher, "current_revision", return_value=1), \
             patch.object(service.publisher, "ready", return_value=ready) as readiness, \
             patch.object(service.publisher, "evidence_link", return_value="synthetic-evidence"):
            if isinstance(ready, tuple):
                readiness.side_effect = ready
            return service.deliver(event_id, device_id="private-device", channel="private-channel",
                                   notifier=notifier, live=live)

    def test_delivery_absence_and_empty_ledger_are_explicit_without_initialization(self):
        self.generation_fixture(profile=self.local_profile())
        before = self.bytes_and_modes()
        report = self.report()
        self.assertEqual(report["database"]["delivery"]["state"], "not_initialized")
        self.assertEqual(before, self.bytes_and_modes())
        service, _event, notifier = self.delivery_fixture()
        before = self.bytes_and_modes()
        report = self.report()
        self.assertEqual(report["database"]["delivery"]["state"], "empty")
        self.assertFalse(any(report["database"]["delivery"]["counts"].values()))
        self.assertEqual(before, self.bytes_and_modes())
        notifier.send.assert_not_called()

    def test_unknown_delivery_is_observed_without_notifier_replay_or_private_fields(self):
        self.generation_fixture(profile=self.local_profile())
        service, event, notifier = self.delivery_fixture()
        notifier.send.side_effect = RuntimeError("private-notifier-error")
        self.assertEqual(self.send_delivery(service, event, notifier).status, "unknown")
        before = self.bytes_and_modes()
        for _ in range(2):
            report = self.report()
            self.assertEqual(report["state"], "unknown")
            self.assertEqual(report["database"]["delivery"]["state"], "unknown")
            self.assertEqual(report["database"]["delivery"]["counts"]["unknown"], 1)
        self.assertEqual(before, self.bytes_and_modes())
        notifier.send.assert_called_once()
        for private in (event, "private-title", "private-body", "private-item", "private-evidence",
                        "private-device", "private-channel", "private-notifier-error"):
            self.assertNotIn(private, json.dumps(report))

    def test_interrupted_sending_remains_unknown_observation_without_reconciliation(self):
        self.generation_fixture(profile=self.local_profile())
        service, event, notifier = self.delivery_fixture()
        notifier.send.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.send_delivery(service, event, notifier)
        before = self.bytes_and_modes()
        report = self.report()
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(report["database"]["delivery"]["state"], "unknown")
        self.assertEqual(report["database"]["delivery"]["counts"]["sending"], 1)
        self.assertEqual(before, self.bytes_and_modes())
        self.assertEqual(service.ledger.one("SELECT status FROM reader_deliveries")[0], "sending")
        notifier.send.assert_called_once()

    def test_deferred_delivery_is_pending_without_a_notifier_call(self):
        self.generation_fixture(profile=self.local_profile())
        service, event, notifier = self.delivery_fixture()
        self.assertEqual(self.send_delivery(service, event, notifier, ready=(True, True, False)).status, "deferred")
        report = self.report()
        self.assertEqual(report["state"], "pending")
        self.assertEqual(report["database"]["delivery"]["state"], "deferred")
        self.assertEqual(report["database"]["delivery"]["counts"]["deferred"], 1)
        notifier.send.assert_not_called()

    def test_terminal_delivery_counts_preserve_rejection_without_a_permanent_health_gate(self):
        self.generation_fixture(profile=self.local_profile())
        for status in ("rejected", "accepted", "silent"):
            service, event, notifier = self.delivery_fixture(status=status, event="synthetic-" + status)
            result = self.send_delivery(service, event, notifier, live=status != "silent")
            self.assertEqual(result.status, status)
            if status == "silent":
                notifier.send.assert_not_called()
            else:
                notifier.send.assert_called_once()
            report = self.report()
            self.assertEqual(report["state"], "healthy")
            self.assertEqual(report["database"]["delivery"]["state"], "rejected")
            self.assertEqual(report["database"]["delivery"]["counts"][status], 1)
        self.assertEqual(service.ledger.all("SELECT * FROM reader_open_observations"), [])
        self.assertEqual(service.ledger.all("SELECT * FROM reader_known"), [])

    def test_accepted_and_silent_are_recorded_outcomes_without_read_or_known_evidence(self):
        self.generation_fixture(profile=self.local_profile())
        for status in ("accepted", "silent"):
            service, event, notifier = self.delivery_fixture(event="synthetic-" + status)
            self.send_delivery(service, event, notifier, live=status != "silent")
        report = self.report()
        self.assertEqual(report["state"], "healthy")
        self.assertEqual(report["database"]["delivery"]["state"], "recorded")
        self.assertEqual(report["database"]["delivery"]["counts"]["accepted"], 1)
        self.assertEqual(report["database"]["delivery"]["counts"]["silent"], 1)
        self.assertEqual(service.ledger.all("SELECT * FROM reader_open_observations"), [])
        self.assertEqual(service.ledger.all("SELECT * FROM reader_known"), [])

    def test_delivery_inspection_reads_status_only_and_performs_no_writes(self):
        self.generation_fixture(profile=self.local_profile())
        service, event, notifier = self.delivery_fixture(status="unknown")
        self.send_delivery(service, event, notifier)
        real_connect = sqlite3.connect
        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            def authorize(action, table, column, _db, _trigger):
                if action == sqlite3.SQLITE_READ and (
                        table in {"reader_attention_events", "reader_delivery_attempts"}
                        or table == "reader_deliveries" and column not in {"status", ""}):
                    return sqlite3.SQLITE_DENY
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                              sqlite3.SQLITE_CREATE_TABLE}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn
        before = self.bytes_and_modes()
        with patch("core.semantic.health.sqlite3.connect", side_effect=connect):
            report = self.report()
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(before, self.bytes_and_modes())
        notifier.send.assert_called_once()

    def test_candidate_delivery_is_not_probed_or_promoted_to_active(self):
        self.generation_fixture(active=False, activated=False, profile=self.local_profile())
        service, event, notifier = self.delivery_fixture(status="unknown")
        self.send_delivery(service, event, notifier)
        before = self.bytes_and_modes()
        report = self.report()
        self.assertEqual(report["state"], "candidate")
        self.assertEqual(report["active_state"], "inactive")
        self.assertEqual(report["database"], {"state": "not_active"})
        self.assertEqual(before, self.bytes_and_modes())
        notifier.send.assert_called_once()

    def test_invalid_delivery_status_fails_closed_without_echoing_the_value(self):
        self.generation_fixture(profile=self.local_profile())
        service, event, notifier = self.delivery_fixture()
        self.send_delivery(service, event, notifier)
        with self.store.transaction() as conn:
            conn.execute("UPDATE reader_deliveries SET status='private-malformed-status'")
        before = self.bytes_and_modes()
        report = self.report()
        self.assertEqual(report["state"], "blocked")
        self.assertEqual(report["database"]["delivery"]["state"], "invalid")
        self.assertEqual(report["database"]["delivery"]["counts"]["invalid"], 1)
        self.assertNotIn("private-malformed-status", json.dumps(report))
        self.assertEqual(before, self.bytes_and_modes())

    def test_healthy_read_is_content_free_and_does_not_create_sidecars_or_change_permissions(self):
        self.generation_fixture()
        self.budget_fixture()
        before = self.bytes_and_modes()
        report = self.report()
        self.assertEqual(report["state"], "healthy")
        self.assertEqual(report["schema"], HEALTH_SCHEMA)
        self.assertEqual(report["checkpoints"]["captured_shards"], 2)
        self.assertEqual(report["checkpoints"]["lagging_shards"], 0)
        self.assertEqual(report["provider_budget"]["state"], "ready")
        self.assertEqual(before, self.bytes_and_modes())
        serialized = json.dumps(report)
        for private in (str(self.root), self.generation, self.account, self.account_key, "synthetic-chat", "synthetic-proof"):
            self.assertNotIn(private, serialized)

    def test_missing_generation_is_inactive_without_storage_creation(self):
        self.runtime.mkdir()
        before = list(self.root.iterdir())
        report = self.report()
        self.assertEqual(report["state"], "inactive")
        self.assertEqual(list(self.root.iterdir()), before)

    def test_unavailable_configured_root_is_blocked_without_storage_creation(self):
        before = list(self.root.iterdir())
        with patch("sqlite3.connect", side_effect=AssertionError("unavailable root must not open DB")):
            report = self.report()
        self.assertEqual(report["state"], "blocked")
        self.assertEqual(report["error_codes"], ["semantic_runtime_root_unavailable"])
        self.assertEqual(list(self.root.iterdir()), before)

    def test_candidate_and_retained_generation_are_not_active(self):
        self.generation_fixture(active=False, activated=False)
        self.budget_fixture()
        report = self.report()
        self.assertEqual(report["state"], "candidate")
        self.assertEqual(report["active_state"], "inactive")
        self.assertEqual(report["candidate_count"], 1)
        with self.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json=json_set(metadata_json,'$.activation',json('{}'))")
        report = self.report()
        self.assertEqual(report["candidate_count"], 0)
        self.assertEqual(report["retained_generation_count"], 1)

    def test_half_cutover_blocks_before_database_open(self):
        self.generation_fixture()
        write_json(self.runtime / "cutover.json", {"schema": JOURNAL_SCHEMA, "phase": "checkpoints", "new_manifest": self.manifest})
        with patch("core.semantic.health.sqlite3.connect", side_effect=AssertionError("half-cutover must not open DB")):
            report = self.report()
        self.assertEqual(report["state"], "blocked")
        self.assertEqual(report["active_state"], "blocked")
        self.assertEqual(report["error_codes"], ["cutover_recovery_required"])

    def test_capture_ahead_of_applied_and_durable_pending_queues_are_separate(self):
        self.generation_fixture()
        self.budget_fixture()
        state_store = MonitorStateStore(self.checkpoint)
        vector = _vector(100, 4)
        state_store.commit_capture(self.snapshot.revision, vector, _capture(self.snapshot, vector))
        jobs = JobStore(self.store)
        jobs.ensure("extract", {"private_text": "never emit me"}, {"version": 1})
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO canonical_transactions VALUES('tx',NULL,'[]','[]',1)")
            conn.execute("INSERT INTO projection_outbox VALUES('p','private-item',1,'tx','pending',NULL)")
            conn.execute("INSERT INTO index_outbox VALUES('i','private-item','item',1,'tx','pending',NULL)")
        report = self.report()
        self.assertEqual(report["state"], "pending")
        self.assertEqual(report["checkpoints"]["lagging_shards"], 1)
        self.assertEqual(report["checkpoints"]["max_lag_seconds"], 0)  # Same second still lags.
        self.assertEqual(report["database"]["jobs"]["pending"], 1)
        self.assertEqual(report["database"]["outbox"]["projection"]["pending"], 1)
        self.assertEqual(report["database"]["outbox"]["index"]["pending"], 1)
        self.assertNotIn("never emit me", json.dumps(report))
        self.assertNotIn("private-item", json.dumps(report))

    def test_unknown_job_and_expired_lease_are_observed_without_requeue(self):
        self.generation_fixture()
        self.budget_fixture()
        jobs = JobStore(self.store)
        job = jobs.ensure("extract", {"x": 1}, {"v": 1})
        jobs.claim(job["job_id"], "worker")
        jobs.fail(job["job_id"], "worker", "provider_unknown", unknown=True)
        running = jobs.ensure("extract", {"x": 2}, {"v": 1})
        jobs.claim(running["job_id"], "worker")
        with self.store.transaction() as conn:
            conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (self.now - 1, running["job_id"]))
        before = self.bytes_and_modes()
        report = self.report()
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(report["database"]["jobs"]["unknown"], 1)
        self.assertEqual(report["database"]["jobs"]["expired_leases"], 1)
        self.assertEqual(before, self.bytes_and_modes())

    def test_budget_missing_and_expired_are_distinct_without_constructor(self):
        self.generation_fixture()
        with patch("core.provider_usage.ProviderUsageStore.__init__", side_effect=AssertionError("no ledger initialization")):
            report = self.report()
        self.assertEqual(report["provider_budget"]["state"], "missing")
        self.assertFalse(self.usage.exists())
        self.budget_fixture(now=self.now - 200)
        report = self.report()
        self.assertEqual(report["state"], "blocked")
        self.assertEqual(report["provider_budget"]["state"], "expired")

    def test_fully_local_profile_needs_no_usage_path_or_account(self):
        self.generation_fixture(profile=self.local_profile())
        # Even an unsafe, irrelevant usage destination must remain untouched.
        self.usage.symlink_to(self.root / "protected-source/provider-usage.sqlite")
        before = self.bytes_and_modes()
        with self.no_usage_access():
            report = self.report()
        self.assertEqual(report["state"], "healthy")
        self.assertEqual(report["provider_budget"], {
            "state": "not_required", "ready": True, "required_accounts": 0,
            "counts": {state: 0 for state in ("ready", "missing", "expired", "blocked", "exhausted")},
            "unknown_requests": 0, "sent_requests": 0, "network_requests": 0, "remote_balance_verified": False,
        })
        self.assertEqual(self.bytes_and_modes(), before)
        profile = self.local_profile()
        profile.update(account_id=self.account, secret_name="unused-reference")
        self.set_profile(profile)
        with self.no_usage_access():
            self.assertEqual(self.report()["provider_budget"]["state"], "not_required")

    def test_local_candidate_is_explicitly_not_required_without_usage_database(self):
        self.generation_fixture(active=False, activated=False, profile=self.local_profile())
        with self.no_usage_access():
            report = self.report()
        self.assertEqual(report["state"], "candidate")
        self.assertEqual(report["provider_budget"]["state"], "not_required")
        self.assertTrue(report["provider_budget"]["ready"])
        self.assertFalse(self.usage.exists())

    def test_local_budget_exemption_does_not_hide_unknown_job_outcomes(self):
        self.generation_fixture(profile=self.local_profile())
        jobs = JobStore(self.store)
        job = jobs.ensure("extract", {"synthetic": "input"}, {"version": 1})
        jobs.claim(job["job_id"], "worker")
        jobs.fail(job["job_id"], "worker", "provider_unknown", unknown=True)
        before = self.bytes_and_modes()
        with self.no_usage_access():
            report = self.report()
        self.assertEqual(report["provider_budget"]["state"], "not_required")
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(report["database"]["jobs"]["unknown"], 1)
        self.assertEqual(self.bytes_and_modes(), before)

    def test_only_selected_cloudflare_roles_require_their_account_budgets(self):
        self.generation_fixture()
        vectorize = self.account_key.replace("cf-ai:", "cf-vectorize:")
        self.budget_fixture()
        self.budget_fixture(account_key=vectorize)
        for decision in ("text", "cloudflare"):
            for embedding in ("local", "cloudflare"):
                for index in ("local", "cloudflare"):
                    with self.subTest(decision=decision, embedding=embedding, index=index):
                        profile = {"schema": PROFILE_SCHEMA, "decision": decision, "embedding": embedding,
                                   "vector_index": index, "account_id": self.account,
                                   "secret_name": "synthetic-reference", "vectorize_index": "synthetic-index"}
                        if embedding == "local":
                            profile["local_embedding"] = dict(LOCAL_EMBEDDING)
                        self.set_profile(profile)
                        required = int("cloudflare" in (decision, embedding)) + int(index == "cloudflare")
                        report = self.report()
                        self.assertEqual(report["state"], "healthy")
                        self.assertEqual(report["provider_budget"]["required_accounts"], required)
                        self.assertEqual(report["provider_budget"]["counts"]["ready"], required)
                        self.assertEqual(report["provider_budget"]["state"], "ready" if required else "not_required")

    def test_vectorize_only_does_not_require_ai_budget_or_inherit_its_unknown_requests(self):
        profile = self.local_profile()
        profile.update(vector_index="cloudflare", vectorize_index="synthetic-index", account_id=self.account,
                       secret_name="synthetic-reference")
        self.generation_fixture(profile=profile)
        usage = self.budget_fixture(account_key=self.account_key.replace("cf-ai:", "cf-vectorize:"))
        self.assertEqual(self.report()["state"], "healthy")  # No Workers AI budget exists.
        self.budget_fixture()
        usage.reserve(self.account_key, request_id="unselected-ai", route="decision", upper_bound_units=5,
                      request_fingerprint="c" * 64, generation_id=self.generation)
        usage.mark_sent("unselected-ai")
        usage.mark_unknown("unselected-ai")
        report = self.report()
        self.assertEqual(report["state"], "healthy")
        self.assertEqual(report["provider_budget"]["required_accounts"], 1)
        self.assertEqual(report["provider_budget"]["unknown_requests"], 0)

    def test_missing_or_invalid_profile_cannot_be_mistaken_for_fully_local(self):
        self.generation_fixture(profile=self.local_profile())
        with self.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json=json_remove(metadata_json,'$.provider_profile')")
        with self.no_usage_access():
            report = self.report()
        self.assertEqual(report["state"], "blocked")
        self.assertEqual(report["provider_budget"]["state"], "profile_missing")
        invalid = [None, {}, json.dumps(self.local_profile()), {"account_id": self.account, "vector_index": "local"},
                   {**self.local_profile(), "schema": "unknown"},
                   {key: value for key, value in self.local_profile().items() if key != "decision"},
                   {**self.local_profile(), "embedding": "guess-local"},
                   {**self.local_profile(), "decision": "cloudflare", "account_id": self.account},
                   {**self.local_profile(), "vector_index": "cloudflare", "account_id": self.account,
                    "secret_name": "synthetic-reference"},
                   {**self.local_profile(), "embedding": "cloudflare", "account_id": self.account,
                    "secret_name": "synthetic-reference"},
                   {**self.local_profile(), "local_embedding": None},
                   {**self.local_profile(), "local_embedding": {}},
                   {**self.local_profile(), "unrecognized": "field"}]
        for change in ({"endpoint": "https://example.invalid:443/v1/embeddings"}, {"model": ""},
                       {"dimensions": True}, {"context_window_tokens": 64}, {"max_batch_size": False},
                       {"api": "ollama"}, {"document_template": "{secret}"}, {"timeout": 0}):
            profile = self.local_profile()
            profile["local_embedding"].update(change)
            invalid.append(profile)
        for profile in invalid:
            with self.subTest(profile=profile):
                self.set_profile(profile)
                with self.no_usage_access():
                    report = self.report()
                self.assertEqual(report["state"], "blocked")
                self.assertEqual(report["provider_budget"]["state"], "profile_invalid")
                self.assertFalse(report["provider_budget"]["ready"])

    def test_duplicate_profile_evidence_is_rejected_without_budget_inspection(self):
        self.generation_fixture(profile=self.local_profile())
        value = json.dumps(self.local_profile())
        metadata = '{"activation":{},"provider_profile":' + value + ',"provider_profile":' + value + '}'
        with self.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET metadata_json=?", (metadata,))
        with self.no_usage_access():
            report = self.report()
        self.assertEqual(report["provider_budget"]["state"], "profile_invalid")
        self.assertEqual(report["state"], "blocked")

    def test_fresh_local_health_import_and_read_has_no_runtime_provider_credentials_or_dns(self):
        self.generation_fixture(profile=self.local_profile())
        script = '''
import builtins, json, os, socket, sys
actual_import = builtins.__import__
def execution_dependency(name):
    return name == "ai" or name.startswith("ai.") or name in {"core.semantic.runtime", "core.keychain", "core.key_extractor", "core.provider_usage"}
def checked_import(name, *args, **kwargs):
    if execution_dependency(name):
        raise AssertionError("health imported an execution dependency")
    return actual_import(name, *args, **kwargs)
builtins.__import__ = checked_import
def no_network(*args, **kwargs):
    raise AssertionError("health attempted network or DNS")
socket.getaddrinfo = socket.socket = socket.create_connection = no_network
def no_usage(original):
    def checked(path, *args, **kwargs):
        if "provider-usage.sqlite" in str(path) or "protected-source" in str(path):
            raise AssertionError("health inspected an unneeded or protected path")
        return original(path, *args, **kwargs)
    return checked
for name in ("stat", "lstat", "open"):
    setattr(os, name, no_usage(getattr(os, name)))
from core.semantic.health import semantic_health
report = semantic_health(json.loads(sys.argv[1]), usage_path=sys.argv[2], now=1800000000.0)
assert report["state"] == "healthy", report
assert report["provider_budget"]["state"] == "not_required", report
assert report["provider_budget"]["ready"] is True, report
assert not any(execution_dependency(name) for name in sys.modules), "execution dependency loaded indirectly"
print("local health dependency boundary OK")
'''
        result = subprocess.run([sys.executable, "-c", script, json.dumps(self.config), str(self.usage)],
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(result.stdout.strip(), "local health dependency boundary OK")

    def test_unknown_provider_outcome_is_reported_without_retry(self):
        self.generation_fixture()
        usage = self.budget_fixture()
        usage.reserve(self.account_key, request_id="unknown-request", route="decision", upper_bound_units=5,
                      request_fingerprint="b" * 64, generation_id=self.generation)
        usage.mark_sent("unknown-request")
        usage.mark_unknown("unknown-request")
        before = self.bytes_and_modes()
        report = self.report()
        self.assertEqual(report["state"], "unknown")
        self.assertEqual(report["provider_budget"]["unknown_requests"], 1)
        self.assertEqual(before, self.bytes_and_modes())

    def test_wal_counts_are_current_and_read_does_not_change_database_files(self):
        self.generation_fixture()
        self.budget_fixture()
        with closing(self.store.connect()) as writer:
            writer.execute("UPDATE generation_manifest SET revision=37")
            writer.commit()
            before = self.bytes_and_modes()
            report = self.report()
            self.assertEqual(report["database"]["revision"], 37)
            self.assertEqual(before, self.bytes_and_modes())

    def test_queries_never_read_source_bodies_or_model_artifacts(self):
        self.generation_fixture()
        self.budget_fixture()
        real_connect = sqlite3.connect
        forbidden = {("source_records", "text"), ("source_records", "payload_json"),
                     ("source_spans", "excerpt"), ("observations", "title"), ("content_units", "text"),
                     ("items", "title"), ("jobs", "inputs_json"), ("accepted_job_artifacts", "artifact_json"),
                     ("reader_projections", "machine_body"), ("reader_text_artifacts", "artifact_json")}
        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            def authorize(action, table, column, _db, _trigger):
                if action == sqlite3.SQLITE_READ and (table, column) in forbidden:
                    return sqlite3.SQLITE_DENY
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_CREATE_TABLE}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn
        with patch("core.semantic.health.sqlite3.connect", side_effect=connect):
            self.assertEqual(self.report()["state"], "healthy")

    def test_missing_active_database_and_changed_generation_never_fall_back(self):
        self.generation_fixture()
        self.budget_fixture()
        other = self.database.with_name("retained.sqlite")
        self.database.rename(other)
        report = self.report()
        self.assertEqual(report["state"], "blocked")
        self.assertEqual(report["error_codes"], ["active_database_binding_invalid"])
        self.assertFalse(self.database.exists())
        other.rename(self.database)
        with self.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET generation_id='different-generation'")
        self.assertEqual(self.report()["error_codes"], ["semantic_health_generation_invalid"])

    def test_partial_checkpoint_install_and_invalid_receipt_evidence_stay_blocked(self):
        self.generation_fixture()
        self.budget_fixture()
        self.checkpoint.write_text('{"schema":"we-groupchat-obsidian.monitor-state.v1","revision":1,"last_checked_ts":100}')
        self.assertEqual(self.report()["checkpoints"]["state"], "blocked")
        self.assertEqual(self.report()["checkpoints"]["legacy_chats"], 1)
        with closing(sqlite3.connect(self.usage)) as conn:
            conn.execute("UPDATE provider_budgets SET confirmation_id=''")
            conn.commit()
        self.assertEqual(self.report()["error_codes"], ["semantic_health_budget_invalid"])

    def test_writer_during_idle_snapshot_invalidates_composite_health(self):
        self.generation_fixture()
        self.budget_fixture()
        from core.semantic.health import _manifest
        def changed(conn, generation):
            value = _manifest(conn, generation)
            with self.store.transaction() as writer:
                writer.execute("UPDATE generation_manifest SET revision=revision+1")
            return value
        with patch("core.semantic.health._manifest", side_effect=changed):
            report = self.report()
        self.assertEqual(report["state"], "blocked")
        self.assertEqual(report["error_codes"], ["semantic_health_changed_during_inspection"])

    def test_json_config_failure_is_machine_readable_and_opens_no_generation(self):
        import scripts.health_check as cli
        from core.config import ConfigError
        with patch("scripts.health_check.load_config", side_effect=ConfigError("config_invalid_json")), \
             patch("scripts.health_check.semantic_health", side_effect=AssertionError("invalid config")):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["--semantic-json"]), 2)
        self.assertEqual(json.loads(output.getvalue()), {"schema": HEALTH_SCHEMA, "state": "blocked",
                                                       "error_codes": ["config_invalid_json"]})

    def test_unknown_schema_corrupt_and_symlink_fail_closed(self):
        self.generation_fixture()
        self.budget_fixture()
        with self.store.transaction() as conn:
            conn.execute("UPDATE generation_manifest SET schema='unknown'")
        self.assertEqual(self.report()["error_codes"], ["semantic_health_generation_invalid"])
        self.database.write_bytes(b"broken")
        self.assertEqual(self.report()["state"], "blocked")
        moved = self.database.with_name("retained.sqlite")
        self.database.rename(moved)
        self.database.symlink_to(moved)
        self.assertEqual(self.report()["error_codes"], ["rebuild_symlink"])

    def test_source_overlap_rejected_before_any_metadata_lookup(self):
        self.config["semantic_runtime_root"] = self.config["db_dir"]
        with patch("pathlib.Path.stat", side_effect=AssertionError("protected stat")), \
             patch("os.lstat", side_effect=AssertionError("protected lstat")):
            report = self.report()
        self.assertEqual(report["error_codes"], ["semantic_health_source_overlap"])

    def test_json_entrypoint_calls_no_source_provider_credentials_or_native_probe(self):
        import scripts.health_check as cli
        self.generation_fixture()
        self.budget_fixture()
        self.config["monitor_chats"] = [{"username": "synthetic-chat"}]
        patches = ["core.config.load_config", "core.wechat_db.WeChatDB", "core.key_extractor.get_cached_keys",
                   "core.key_extractor.check_new_databases", "core.keychain.load_key", "ai.factory.create_provider",
                   "core.semantic.runtime.SemanticRuntime.active", "core.semantic.store.SemanticStore.__init__",
                   "core.provider_usage.ProviderUsageStore.__init__", "urllib.request.urlopen", "socket.socket",
                   "scripts.health_check.is_wechat_signed", "scripts.health_check.launch_agent_report"]
        with ExitStack() as stack:
            for target in patches:
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            factory = stack.enter_context(patch("scripts.health_check.ConfigStore"))
            factory.return_value.inspect.return_value = self.config
            stack.enter_context(patch("scripts.health_check.state_file_for_chat", return_value=str(self.checkpoint)))
            stack.enter_context(patch("core.semantic.health.DATA_DIR", self.root))
            stack.enter_context(patch("core.semantic.health.time.time", return_value=self.now))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["--semantic-json"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["state"], "healthy")
        self.assertEqual(report["provider_budget"]["network_requests"], 0)
        factory.return_value.inspect.assert_called_once_with()
        factory.return_value.load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
