"""Composition used by both a rebuild and the long-lived menu application."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid
from zoneinfo import ZoneInfo

from ai.factory import create_provider
from ai.semantic_provider import LegacyTextProvider, ProviderError, TextDecisionProvider
from ai.cloudflare_provider import CloudflareClient, CloudflareDecisionProvider, CloudflareEmbeddingProvider
from ai.local_embedding_provider import LocalEmbeddingProvider
from ai.vector_index import LocalCosineIndex, CloudflareVectorizeIndex
from core.config import (ConfigError, ConfigStore, DATA_DIR, DEFAULT_CONFIG,
                         LEGACY_CONFIG_FILE, LEGACY_DATA_CONFIG_FILE)
from core.keychain import load_key
from core.provider_usage import ProviderUsageStore
from .contracts import SemanticStoreError, digest
from .engine import SemanticEngine, SemanticValidationError
from .retrieval import HybridRetriever
from .projections import ReadingLedger, ProjectionPublisher, CanonicalReadingAdapter, BodyPlanWriter
from .controls import ReaderControls, CoreSearchAdapter
from .briefing import BriefChange, BriefingService
from .delivery import DeliveryService
from .rebuild import GenerationRegistry, RebuildError
from .store import SemanticStore
from .source import SemanticCapture, MonitorReceiptBridge
from .scheduling import scheduled_providers
from core.monitor_state import MONITOR_V3_STATE_SCHEMA, monitor_state_fingerprint
from core.source_adapter import decode_cursor_token


PROFILE_SCHEMA = "we-groupchat-obsidian.semantic-providers.v1"


def legacy_command_blocked() -> bool:
    """Reject legacy CLI work using only existing config and generation metadata.

    Inspection never initializes config, constructs a runtime/provider, or
    opens source/output databases. A missing active pin (including a completed
    rollback to legacy state) preserves the old command's behavior. Ambiguous
    config or cutover evidence fails closed before the caller's legacy loader.
    """
    code = "semantic_legacy_config_unavailable"
    try:
        config = None
        for path in (None, LEGACY_DATA_CONFIG_FILE, LEGACY_CONFIG_FILE):
            config = ConfigStore(path).inspect()
            if config is not None:
                break
        if config is None:
            config = DEFAULT_CONFIG
        code = "semantic_legacy_cutover_blocked"
        root = Path(os.path.abspath(os.path.expanduser(
            config.get("semantic_runtime_root") or str(Path(DATA_DIR) / "semantic"))))
        # Reject a misconfigured metadata root before any protected-source stat.
        if config.get("db_dir"):
            source = Path(os.path.abspath(os.path.expanduser(config["db_dir"])))
            if root == source or root.is_relative_to(source) or source.is_relative_to(root):
                raise ValueError("semantic_metadata_source_overlap")
        if GenerationRegistry(root, require_available=bool(config.get("semantic_runtime_root"))).pin() is None:
            return False
        code = "semantic_legacy_command_blocked"
    except (ConfigError, RebuildError, OSError, TypeError, ValueError):
        pass
    print(json.dumps({
        "state": "blocked", "code": code,
        "next": "Use the menu app for current semantic knowledge. Inspect rebuild state with "
                "scripts/rebuild_knowledge.py status <run_id>; repair unavailable state or complete an "
                "explicit rollback to legacy state before using legacy commands.",
    }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return True


def validate_profile(profile):
    fields = {"schema", "decision", "embedding", "vector_index", "account_id", "vectorize_index", "secret_name",
              "local_embedding"}
    if (not isinstance(profile, dict) or profile.get("schema") != PROFILE_SCHEMA
            or set(profile) - fields or profile.get("decision") not in ("cloudflare", "text")
            or profile.get("embedding") not in ("local", "cloudflare")
            or profile.get("vector_index") not in ("local", "cloudflare")
            or (profile.get("vector_index") == "cloudflare" and not profile.get("vectorize_index"))):
        raise SemanticStoreError("semantic_provider_profile_invalid")
    uses_cloudflare = any(profile[field] == "cloudflare" for field in ("decision", "embedding", "vector_index"))
    if uses_cloudflare:
        account, secret = profile.get("account_id"), profile.get("secret_name")
        if (not isinstance(account, str) or len(account) != 32
                or any(character not in "0123456789abcdefABCDEF" for character in account)
                or not isinstance(secret, str) or not secret.strip()):
            raise SemanticStoreError("semantic_provider_profile_invalid")
    if profile["vector_index"] == "cloudflare" and not isinstance(profile["vectorize_index"], str):
        raise SemanticStoreError("semantic_provider_profile_invalid")
    if profile["embedding"] == "local":
        try:
            LocalEmbeddingProvider.from_config(profile.get("local_embedding"))
        except ProviderError:
            raise SemanticStoreError("semantic_provider_profile_invalid") from None
    elif "local_embedding" in profile:
        raise SemanticStoreError("semantic_provider_profile_invalid")
    return dict(profile)


@dataclass(frozen=True)
class ProviderRoles:
    text: object
    decision: object
    embedding: object
    index: object
    scheduler: object = None

    def scheduled(self, config):
        if self.scheduler is not None:
            return self
        text, decision, embedding, scheduler = scheduled_providers(
            self.text, self.decision, self.embedding,
            limits=config.get("semantic_provider_limits"),
            output_tokens=config.get("semantic_text_output_tokens", 16384))
        return ProviderRoles(text, decision, embedding, self.index, scheduler)

    def preflight(self):
        """Check only selected Cloudflare accounts; local routes need no budget.

        This is not a local service probe. Text retains its configured route's
        own authorization and does not acquire a fictitious Cloudflare limit.
        """
        checks = []
        for provider in (self.embedding, self.decision, self.index):
            client = getattr(provider, "client", None)
            if client is None:
                continue
            key = client.vectorize_account_key if provider is self.index else client.account_key
            if key not in {entry[0] for entry in checks}:
                checks.append((key, client.usage_store.budget_status(key)))
        return {"ready": all(status["ready"] for _, status in checks),
                "budgets": [status for _, status in checks],
                "admission": self.scheduler.status() if self.scheduler else {}}

    @classmethod
    def from_profile(cls, config, profile, *, usage_path=None, local_transport=None,
                     cloudflare_transport=None):
        profile = validate_profile(profile)
        selected = dict(config)
        selected["ai_provider"] = config.get("monitor_ai_provider") or config.get("ai_provider")
        selected["ai_model"] = config.get("monitor_ai_model") or config.get("ai_model")
        selected["ai_thinking"] = bool(config.get("monitor_ai_thinking", False))
        selected["ai_timeout_seconds"] = config.get("monitor_ai_timeout_seconds", 90)
        native = create_provider(selected)
        if getattr(native, "_commandcode_gateway", False) is True:
            # Semantic consumers all validate JSON objects. Keep ordinary
            # prose summaries on the provider's normal text response format.
            native.json_object_output = True
        if hasattr(native, "max_output_tokens"):
            native.max_output_tokens = config.get("semantic_text_output_tokens", 16384)
        if hasattr(getattr(native, "client", None), "max_retries"):
            # This semantic composition owns retries through durable jobs.
            # SDK retries would hide sends from admission and usage accounting.
            native.client.max_retries = 0
        writer = LegacyTextProvider(native, provider_name=selected["ai_provider"], model=native.model,
                                    local=selected["ai_provider"] == "ollama")
        client = None
        if any(profile[field] == "cloudflare" for field in ("decision", "embedding", "vector_index")):
            key = load_key(profile["secret_name"])
            if not key:
                raise SemanticStoreError("semantic_cloudflare_credential_missing")
            usage = ProviderUsageStore(usage_path or str(Path(DATA_DIR) / "provider-usage.sqlite"))
            client = CloudflareClient(profile["account_id"], key, usage, transport=cloudflare_transport)
        decision = CloudflareDecisionProvider(client) if profile["decision"] == "cloudflare" else TextDecisionProvider(writer)
        embedding = (CloudflareEmbeddingProvider(client) if profile["embedding"] == "cloudflare"
                     else LocalEmbeddingProvider.from_config(profile["local_embedding"], transport=local_transport))
        index = (CloudflareVectorizeIndex(client, profile["vectorize_index"])
                 if profile["vector_index"] == "cloudflare" else LocalCosineIndex())
        return cls(writer, decision, embedding, index).scheduled(config)


class SemanticRuntime:
    def __init__(self, store, roles, projection_root, *, config=None):
        self.store, self.config = store, dict(config or {})
        self.roles = roles = roles.scheduled(self.config)
        self.scheduler = roles.scheduler
        shared_root = self.config.get("semantic_runtime_root")
        self.retrieval = HybridRetriever(store, roles.embedding, roles.index, decision_provider=roles.decision,
            shared_lock_root=Path(shared_root) / "index-locks" if shared_root else None, scheduler=self.scheduler)
        self.engine = SemanticEngine(store, roles.text, roles.decision, retriever=self.retrieval)
        self.ledger = ReadingLedger(store.connect, store.generation_id)
        self.adapter = CanonicalReadingAdapter(store)
        resource_roots = [self.config["attachment_archive_root"]] if self.config.get("attachment_archive_root") else []
        self.publisher = ProjectionPublisher.for_store(store, projection_root, resource_roots=resource_roots)
        self.controls = ReaderControls(self.ledger, self.publisher, search=CoreSearchAdapter(store, self.retrieval))
        self.body_writer = BodyPlanWriter(roles.text, store.generation_id)
        self.briefing = BriefingService(self.ledger, self.publisher, self.controls, roles.text,
                                       timezone=self.config.get("daily_digest_timezone", "Asia/Shanghai"))
        self.delivery = DeliveryService(self.ledger, self.publisher, self.controls)

    @classmethod
    def active(cls, config):
        root = Path(config.get("semantic_runtime_root") or Path(DATA_DIR) / "semantic")
        pin = GenerationRegistry(root, require_available=bool(config.get("semantic_runtime_root"))).pin()
        if pin is None:
            return None
        store = SemanticStore(pin.database, pin.generation_id)
        with closing(store.connect()) as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM generation_manifest").fetchone()[0])
        roles = ProviderRoles.from_profile(config, metadata.get("provider_profile"))
        if (roles.embedding.profile.profile_id != pin.embedding_profile_id
                or roles.index.index_identity != pin.index_generation_id):
            raise SemanticStoreError("active_provider_profile_mismatch")
        return cls(store, roles, pin.projection_root, config=config)

    def process(self, batch_id):
        # Stale proposals retain extraction and receive a new read-context key.
        with closing(self.store.connect()) as conn:
            done = conn.execute("SELECT receipt_json FROM processing_receipts WHERE batch_id=?", (batch_id,)).fetchone()
        if done:
            return json.loads(done[0])
        for revision in range(1, 4):
            try:
                result = self.engine.process_batch(batch_id, plan_revision=revision)
                if result.get("status") in {"deferred", "unclassified"}:
                    raise SemanticStoreError("semantic_processing_pending")
                return self.store.complete_batch(batch_id)
            except (SemanticStoreError, SemanticValidationError) as exc:
                retryable_snapshot = (
                    isinstance(exc, SemanticStoreError) and exc.code == "semantic_stale_read_set"
                ) or (
                    isinstance(exc, SemanticValidationError) and exc.code == "semantic_snapshot_changed"
                )
                if not retryable_snapshot or revision == 3:
                    raise
        raise SemanticStoreError("semantic_processing_pending")

    def check_monitor(self, monitor, *, dry_run=False):
        """The active monitor path. Applied progress depends on durable core work.

        Presentation conflicts and delayed vector publication remain independent
        downstream work. A failed provider leaves the captured rows available
        and the applied prefix unchanged.
        """
        snapshot = monitor.state_store.inspect()
        username = str(monitor.config.get("monitor_chat_username") or "")
        if not username:
            raise SemanticStoreError("semantic_chat_missing")
        if snapshot.schema != MONITOR_V3_STATE_SCHEMA and not dry_run:
            self._initialize_selection(monitor, snapshot, username)
            return {"status": "initialized", "message": "新选群聊已从当前选择时间开始；既有群聊进度保留"}
        if (snapshot.schema != MONITOR_V3_STATE_SCHEMA or
                snapshot.data.get("semantic_generation_id") != self.store.generation_id or
                snapshot.data.get("chat_id") != username):
            raise SemanticStoreError("semantic_checkpoint_migration_required")
        if dry_run:
            return {"status": "semantic_dry_run", "message": "已检查语义代与进度绑定；未读取消息或调用模型"}
        bridge = MonitorReceiptBridge(self.store)
        data = snapshot.data
        # Reconcile the one crash window after checkpoint CAS but before this
        # generation-local delivery marker. The checkpoint receipt is authority.
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS semantic_applied_batches(batch_id TEXT PRIMARY KEY,state_revision INTEGER NOT NULL)")
            prior = data["processing_receipt"]
            if prior:
                row = conn.execute("SELECT batch_id FROM processing_receipts WHERE receipt_id=?", (prior["receipt_id"],)).fetchone()
                if row:
                    conn.execute("INSERT OR IGNORE INTO semantic_applied_batches VALUES(?,?)", (row[0], snapshot.revision))
        batches = self._live_batches(username)
        previous_capture_id = (data["capture_receipt"] or {}).get("receipt_id")
        captured = next((batch for batch in batches
                         if batch["input_cursors"] == data["captured_cursors"]
                         and batch["receipt_id"] != previous_capture_id
                         and batch["receipt_id"] not in self._applied_capture_ids()), None)
        if captured is None:
            capture = SemanticCapture(self.store, monitor.db,
                page_size=max(1, min(2000, int(monitor.config.get("monitor_max_messages_per_run", 200)))))
            captured = capture.page(username, data["captured_cursors"], snapshot_id="live-" + uuid.uuid4().hex)
        receipt = bridge.capture(snapshot, captured["batch_id"])
        snapshot = monitor.state_store.commit_capture(snapshot.revision, captured["output_cursors"], receipt)
        self._write_live_observation(snapshot, "captured")
        backoff = monitor._ai_backoff_result(snapshot.data)
        if backoff:
            return backoff
        for batch in self._live_batches(username):
            with closing(self.store.connect()) as conn:
                applied = conn.execute("SELECT 1 FROM semantic_applied_batches WHERE batch_id=?", (batch["batch_id"],)).fetchone()
            if applied or batch["input_cursors"] != snapshot.data["source_cursors"]:
                continue
            if not self._within_capture(batch["output_cursors"], snapshot.data["captured_cursors"]):
                continue
            try:
                self.process(batch["batch_id"])
            except Exception as exc:
                from ai.semantic_provider import ProviderError
                if isinstance(exc, ProviderError) and exc.retryable:
                    monitor._commit_retryable_ai_failure(snapshot, exc)
                raise
            receipt = bridge.processing(snapshot, batch["batch_id"])
            snapshot = monitor.state_store.commit_applied(snapshot.revision, batch["output_cursors"], receipt)
            with self.store.transaction() as conn:
                conn.execute("INSERT OR IGNORE INTO semantic_applied_batches VALUES(?,?)", (batch["batch_id"], snapshot.revision))
            self._write_live_observation(snapshot, "applied")
            if snapshot.data.get("ai_failure_count"):
                cleaned = dict(snapshot.data)
                monitor._clear_ai_failure_state(cleaned)
                snapshot = monitor.state_store.commit(snapshot.revision, cleaned)
            downstream = "ready"
            try:
                index = self.index_all(max_rounds=1)
                projection = self.publish_all()
                if not index["complete"] or not projection["complete"]:
                    downstream = "pending"
            except Exception:
                # The canonical transaction and exact progress receipt already
                # committed. Keep the outbox pending; reader timer resumes it.
                downstream = "pending"
            count = len(batch["members"])
            return {"status": "no_messages" if not count and batch["source_eof"] else "semantic_applied",
                    "source_eof": batch["source_eof"], "raw_message_count": count,
                    "message_count": count, "last_msg_ts": snapshot.data["last_checked_ts"],
                    "semantic_generation_id": self.store.generation_id, "derived_state": downstream,
                    "message": "语义进度已确认"}
        raise SemanticStoreError("semantic_applied_prefix_gap")

    def _initialize_selection(self, monitor, snapshot, username):
        selected_since = monitor.config.get("semantic_selection_started_at")
        if (snapshot.data.get("pending_source_batch") or
                (snapshot.existed and (type(selected_since) is not int or selected_since <= 0))):
            raise SemanticStoreError("semantic_checkpoint_migration_required")
        purpose = "from-now:" + monitor_state_fingerprint(snapshot)
        with closing(self.store.connect()) as conn:
            prior = conn.execute("SELECT batch_id FROM capture_batches WHERE chat_id=? AND purpose=? ORDER BY rowid LIMIT 1",
                                 (username, purpose)).fetchone()
        if prior:
            batch = self.store.get_batch(prior[0])
            # A crash after capture must keep its original start, while a changed
            # source generation still requires the explicit recovery boundary.
            from core.monitor_source import _read_inventory, verify_monitor_source_inventory
            inventory, _ = _read_inventory(monitor.db)
            if (inventory["source_namespace"] != batch["source_namespace"] or
                    inventory["inventory_digest"] != batch["inventory_digest"]):
                raise SemanticStoreError("source_generation_changed")
            verify_monitor_source_inventory(monitor.db, inventory["inventory_digest"], inventory["inventory_revision"])
        else:
            checkpoint = selected_since if type(selected_since) is int and selected_since > 0 else int(monitor.now_func())
            batch = SemanticCapture(self.store, monitor.db).start_boundary(username, checkpoint, monitor_state_fingerprint(snapshot))
        self.process(batch["batch_id"])
        state, receipt = MonitorReceiptBridge(self.store).initialize_from_now(snapshot, batch["batch_id"])
        monitor.state_store.install_v3(snapshot.revision, state, receipt)

    @staticmethod
    def _within_capture(output, captured):
        return (output.keys() == captured.keys() and all(
            value["generation_id"] == captured[key]["generation_id"] and
            decode_cursor_token(value["cursor_token"]) <= decode_cursor_token(captured[key]["cursor_token"])
            for key, value in output.items()))

    def _live_batches(self, username):
        with closing(self.store.connect()) as conn:
            ids = [row[0] for row in conn.execute("SELECT batch_id FROM capture_batches WHERE chat_id=? AND purpose='live' ORDER BY rowid", (username,))]
        return [self.store.get_batch(identity) for identity in ids]

    def _applied_capture_ids(self):
        with closing(self.store.connect()) as conn:
            return {row[0] for row in conn.execute("SELECT b.receipt_id FROM capture_batches b JOIN semantic_applied_batches a USING(batch_id)")}

    def _write_live_observation(self, snapshot, phase):
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS semantic_live_observations(chat_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,state_revision INTEGER NOT NULL,phase TEXT NOT NULL,observed_at REAL NOT NULL,receipt_json TEXT NOT NULL)")
            receipt = snapshot.data["processing_receipt" if phase == "applied" else "capture_receipt"]
            conn.execute("INSERT INTO semantic_live_observations VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET generation_id=excluded.generation_id,state_revision=excluded.state_revision,phase=excluded.phase,observed_at=excluded.observed_at,receipt_json=excluded.receipt_json",
                         (snapshot.data["chat_id"], self.store.generation_id, snapshot.revision, phase,
                          time.time(), json.dumps(receipt, sort_keys=True)))

    def index_all(self, *, max_rounds=10000):
        preparation = self.retrieval.prepare_index()
        for _ in range(max_rounds):
            self.retrieval.confirm_pending()
            result = self.retrieval.index_pending(limit=100, preparation=preparation)
            if result["pending"] == 0:
                return {"receipt_id": str(uuid.uuid4()), "complete": True,
                        "profile_id": self.roles.embedding.profile.profile_id,
                        "index_generation_id": self.roles.index.index_identity}
            if result["embedded"] == 0 and result["indexed"] == 0:
                # Persisted mutation will be confirmed by the next bounded turn;
                # no rapid re-upload and no success inferred from HTTP acceptance.
                return {"receipt_id": str(uuid.uuid4()), "complete": False, "code": "index_confirmation_pending"}
        return {"receipt_id": str(uuid.uuid4()), "complete": False, "code": "index_round_budget_reached"}

    def publish_all(self):
        published, conflicts = 0, 0
        # Targets precede redirect/hub publication so every stable old link works.
        items = self.store.list_items(include_retired=True)
        items.sort(key=lambda item: item["lifecycle"] != "active")
        # Network preparation is independent. Publication and ACK preserve the
        # ordered targets/redirects and each existing filesystem/CAS boundary.
        with self.scheduler.phase():
            for start in range(0, len(items), 100):
                prepared = self.scheduler.map("text", items[start:start + 100], lambda item:
                    self.publisher.prepare_canonical_item(self.store, item["item_id"], self.body_writer,
                        request_id="body:" + digest([self.store.generation_id, item["item_id"], item["content_revision"]])))
                for plan in prepared:
                    receipt = self.publisher.publish_prepared_item(self.store, plan)
                    if receipt.status in {"published", "unchanged"}:
                        published += 1
                    elif receipt.status in {"conflict", "personal"}:
                        conflicts += 1
                    else:
                        raise SemanticStoreError("semantic_projection_incomplete")
        with closing(self.store.connect()) as conn:
            views = [dict(r) for r in conn.execute("SELECT * FROM views")]
            memberships = [dict(r) for r in conn.execute("SELECT * FROM view_memberships WHERE excluded=0")]
        for view in views:
            entries = []
            for member in memberships:
                if member["view_id"] == view["view_id"]:
                    item = self.store.get_item(member["item_id"])
                    if item["lifecycle"] == "active":
                        entries.append((item["item_id"], "", item["title"]))
            self.publisher.publish_view(view["view_id"], view["title"], entries,
                                        revision=view["definition_revision"] + view["override_revision"])
        self.controls.initialize_query(property_key=self.controls.property_mapping(self.config.get("semantic_reader_properties", {}))["query"])
        for item in items:
            if item["lifecycle"] == "active" and self.publisher.ready(item["item_id"], item["content_revision"]):
                self.controls.register_item_actions(item["item_id"], item["content_revision"])
        self.controls.refresh_management()
        return {"receipt_id": str(uuid.uuid4()), "published": published, "conflicts": conflicts,
                "complete": conflicts == 0}

    def poll_controls(self):
        now = datetime.now(timezone.utc).isoformat()
        for item in self.store.list_items():
            self.controls.sync_properties(item["item_id"])
        actions = self.controls.process_management(acted_at=now)
        query = self.controls.run_query()
        return {"actions": len(actions), "query": query.status}

    def brief(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        zone = ZoneInfo(self.config.get("daily_digest_timezone", "Asia/Shanghai"))
        local = now.astimezone(zone)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with closing(self.store.connect()) as conn:
            changes = [dict(row) for row in conn.execute(
                "SELECT e.*,t.actions_json FROM change_events e JOIN canonical_transactions t USING(transaction_id) "
                "WHERE e.created_at>=? AND e.created_at<=? ORDER BY change_id", (start, now.timestamp()))]
        _, edition_id = self.briefing._identity(now)
        considered = self.ledger.one("SELECT i.inputs_json FROM reader_brief_attempts a JOIN reader_brief_inputs i "
            "ON i.generation_id=a.generation_id AND i.input_hash=a.input_hash WHERE a.generation_id=? AND a.edition_id=? "
            "AND a.status IN ('success','silent') ORDER BY a.rowid DESC LIMIT 1", (self.store.generation_id, edition_id))
        previous_inputs = {value["item_id"]: value for value in json.loads(considered[0])} if considered else {}
        latest = {}
        for change in changes:
            latest[change["item_id"]] = change
        candidates = []
        for item_id, change in latest.items():
            item = self.store.get_item(item_id)
            if item["lifecycle"] != "active":
                continue
            snapshot = self.adapter.snapshot(item_id)
            if change["revision"] != snapshot.content_revision:
                raise SemanticStoreError("semantic_brief_stale_snapshot")
            previous_input = previous_inputs.get(item_id)
            if previous_input and previous_input["id"] == str(change["change_id"]):
                restored = dict(previous_input)
                for key in ("conditions", "evidence_ids", "contributing_change_ids"):
                    restored[key] = tuple(restored.get(key, ()))
                candidates.append(BriefChange(**restored))
                continue
            baseline = (previous_input["content_revision"] if previous_input else
                        min(c["revision"] for c in changes if c["item_id"] == item_id) - 1)
            with closing(self.store.connect()) as conn:
                prior = conn.execute("SELECT snapshot_json FROM item_revisions WHERE item_id=? AND revision=?",
                                     (item_id, baseline)).fetchone()
                current = conn.execute("SELECT snapshot_json FROM item_revisions WHERE item_id=? AND revision=?",
                                       (item_id, change["revision"])).fetchone()
                contributing = [dict(row) for row in conn.execute(
                    "SELECT e.*,t.actions_json FROM change_events e JOIN canonical_transactions t USING(transaction_id) "
                    "WHERE e.item_id=? AND e.revision>? AND e.revision<=? ORDER BY e.revision", (item_id, baseline, change["revision"]))]
                after = json.loads(current[0])
                before = json.loads(prior[0]) if prior else {"units": [], "claims": [], "relations": []}
                old_claims = {c["claim_id"]: c for c in before["claims"]}
                new_claims = {c["claim_id"]: c for c in after["claims"]}
                relevant = set(old_claims) | set(new_claims)
                relation_changes = []
                for action in [action for event in contributing for action in json.loads(event["actions_json"])]:
                    relations = [action] if action.get("type") == "CLAIM_RELATION" else action.get("claim_relations", ())
                    for relation in relations:
                        if not {relation["from_claim_id"], relation["to_claim_id"]} & relevant or relation["relation"] == "same_material":
                            continue
                        actual = conn.execute("SELECT * FROM claim_relations WHERE from_claim_id=? AND to_claim_id=? AND relation=?",
                            (relation["from_claim_id"], relation["to_claim_id"], relation["relation"])).fetchone()
                        if actual is None:
                            raise SemanticStoreError("semantic_brief_relation_missing")
                        relation_changes.append(dict(actual))
                related_claims = {}
                for relation in relation_changes:
                    for key in ("from_claim_id", "to_claim_id"):
                        row = conn.execute("SELECT * FROM claims WHERE claim_id=?", (relation[key],)).fetchone()
                        related_claims[row["claim_id"]] = {key.removesuffix("_json"): json.loads(value)
                            if key.endswith("_json") else value for key, value in dict(row).items()}

            # Compare this canonical revision with the last considered version. A
            # late continuation sends only its new/changed units to the editor.
            # Claim status is separate from immutable content-unit wording.
            claim_fields = ("status", "predicates", "condition", "channel", "version", "phase", "subject_id",
                            "case_id", "polarity", "modality", "valid_from", "valid_to", "retracts")
            changed_claims = {id_ for id_ in relevant if
                tuple(old_claims.get(id_, {}).get(key) for key in claim_fields) !=
                tuple(new_claims.get(id_, {}).get(key) for key in claim_fields)}
            claim_spans = {span for id_ in changed_claims for claims in (old_claims, new_claims)
                           for span in claims.get(id_, {}).get("span_ids", ())}
            old_units = {u["unit_id"]: u for u in before["units"]}
            new_units = {u["unit_id"]: u for u in after["units"]}
            affected = {id_ for id_ in set(old_units) | set(new_units) if old_units.get(id_) != new_units.get(id_)}
            affected.update(u["unit_id"] for u in before["units"] + after["units"] if set(u["span_ids"]) & claim_spans)
            def words(units, claims):
                result = []
                for unit in units:
                    if unit["unit_id"] not in affected:
                        continue
                    statuses = {c["status"] for c in claims.values() if set(c["span_ids"]) & set(unit["span_ids"])}
                    prefix = ("【历史陈述，已撤回】" if "retracted" in statuses else
                              "【历史陈述，存在反证】" if "contested" in statuses else "")
                    result.append(prefix + unit["text"])
                return result
            before_text, after_text = words(before["units"], old_claims), words(after["units"], new_claims)
            relation_fields = ("from_item_id", "to_item_id", "relation", "direction", "reason", "evidence")
            old_relations = {r["relation_id"]: r for r in before["relations"]}
            new_relations = {r["relation_id"]: r for r in after["relations"]}
            item_relation_changes = {id_ for id_ in set(old_relations) | set(new_relations) if
                tuple(old_relations.get(id_, {}).get(key) for key in relation_fields) !=
                tuple(new_relations.get(id_, {}).get(key) for key in relation_fields)}
            for relations, text in ((old_relations, before_text), (new_relations, after_text)):
                for id_ in sorted(item_relation_changes & relations.keys()):
                    relation = relations[id_]
                    text.append("关联 " + relation["relation"] + "：" + relation["reason"])

            corrections = []
            labels = {"retracted": "已撤回", "contested": "存在反证"}
            for id_ in sorted(changed_claims):
                status = new_claims.get(id_, {}).get("status")
                if status in labels and old_claims.get(id_, {}).get("status") != status:
                    corrections.append("原陈述" + labels[status] + "，原话仅保留作历史对照。")
            for relation in relation_changes:
                message = "声明关系 " + relation["relation"] + ("：" + relation["reason"] if relation["reason"] else "")
                after_text.append(message)
                if relation["relation"] in {"corrects_or_retracts", "corrects", "retracts", "disputes", "contradicts"}:
                    corrections.append(message)
            delta_claims = {id_: new_claims.get(id_, old_claims.get(id_)) for id_ in sorted(changed_claims)} | related_claims
            scopes = [json.dumps({key: c[key] for key in ("subject_id", "case_id", "channel", "version", "phase", "valid_from", "valid_to")
                                  if c.get(key) is not None}, ensure_ascii=False, sort_keys=True)
                      for c in delta_claims.values()]
            scopes = list(dict.fromkeys(scope for scope in scopes if scope != "{}"))
            conditions = tuple(dict.fromkeys(c["condition"] for c in delta_claims.values() if c.get("condition")))
            evidence_ids = {span for unit in before["units"] + after["units"] if unit["unit_id"] in affected for span in unit["span_ids"]}
            evidence_ids.update(span for c in delta_claims.values() for span in c["span_ids"])
            evidence_ids.update(span for r in relation_changes for span in json.loads(r["evidence_json"]))
            for id_ in item_relation_changes:
                for relation in (old_relations.get(id_), new_relations.get(id_)):
                    if relation:
                        evidence_ids.update(relation["evidence"])
            evidence_ids = set(self.adapter.evidence_span_ids(evidence_ids))
            evidence = tuple(e for e in snapshot.evidence if e.id in evidence_ids)
            candidates.append(BriefChange(str(change["change_id"]), item_id, snapshot.content_revision, item["title"],
                "\n".join(before_text), "\n".join(after_text), tuple(e.id for e in evidence),
                max((e.source_time for e in evidence), default=""),
                datetime.fromtimestamp(change["created_at"], timezone.utc).isoformat(),
                scope="；".join(scopes), correction="；".join(dict.fromkeys(corrections)), conditions=conditions,
                kind=item["kind"], interest=self.config.get("monitor_topic", ""),
                relation_change=bool(relation_changes or item_relation_changes), before_revision=baseline,
                contributing_change_ids=tuple(str(event["change_id"]) for event in contributing)))
        return self.briefing.build(candidates, now=now)

    def brief_if_due(self, *, now=None):
        """Create or revise today's edition after the configured local time."""
        if self.config.get("daily_digest_enabled") is False:
            return None
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(self.briefing.zone)
        from core.daily_digest import _parse_digest_time
        hour, minute = _parse_digest_time(self.config.get("daily_digest_time"))
        if ((local.hour, local.minute) < (hour, minute)
                and self.briefing.edition(local.date().isoformat()) is None):
            return None
        return self.brief(now=now)
