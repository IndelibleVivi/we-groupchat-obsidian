"""Topic monitoring for WeChat chats.

The monitor is deliberately read-only: it reads decrypted DB cache data,
calls an AI model to classify new messages, and optionally writes local hit
records. It never activates WeChat or sends messages.
"""
import json
import hashlib
import os
import re
import time
from datetime import datetime

from .config import DATA_DIR
from .knowledge import (
    HUMAN_AI_INTIMACY_CATEGORIES,
    HUMAN_AI_INTIMACY_PROFILE,
    KnowledgeStore,
    KNOWLEDGE_DB,
    RELATION_NOTIFY,
    TAXONOMY_PROFILES,
    build_message_hash,
    extract_file_refs,
    normalize_candidate,
    normalize_relation,
    vault_chat_name,
)
from .taxonomy_assignment import resolve_taxonomy_profile
from .link_preview import is_wechat_record_url
from .url_safety import URL_RE, redact_urls_in_text
from .api_errors import is_retryable_ai_error, normalize_ai_error
from .review_queue import ReviewQueue
from .daily_digest import source_window_dates
from .monitor_state import MonitorStateError, MonitorStateStore
from .monitor_source import (
    MonitorSourceError,
    freeze_monitor_source_batch,
    pending_monitor_source_batch,
    verify_pending_monitor_source_batch,
    initialize_monitor_source_state,
    read_monitor_source_batch,
    supports_monitor_source_cursors,
    verify_monitor_source_inventory,
)

STATE_FILE = os.path.join(DATA_DIR, "monitor_state.json")
STATE_DIR = os.path.join(DATA_DIR, "monitor_state")
HITS_DIR = os.path.join(DATA_DIR, "monitor_hits")


class MonitorDecisionError(RuntimeError):
    """Content-free failure at the provider decision acceptance boundary."""

    code = "ai_invalid_response"

    def __init__(self):
        super().__init__(self.code)


class MonitorConfigError(RuntimeError):
    """Raised when the monitor is missing required user configuration."""


def load_state(path=STATE_FILE):
    """Compatibility reader backed by the canonical state store."""
    return MonitorStateStore(path).read().data


def save_state(state, path=STATE_FILE):
    """Compatibility writer using one locked atomic update."""
    MonitorStateStore(path).update(lambda _current: dict(state))


def initialize_state_if_needed(path=STATE_FILE, now_func=time.time):
    """Set the first monitor checkpoint to now, avoiding historical floods."""
    snapshot = MonitorStateStore(path).initialize_if_absent({
        "last_checked_ts": now_func(),
    })
    return not snapshot.existed


def reset_state_to_now(path=STATE_FILE, now_func=time.time):
    """Reset the monitor checkpoint after changing target or interest text."""
    def mutate(state):
        state["last_checked_ts"] = now_func()
        for key in (
            "pending_source_batch",
            "source_cursors",
            "source_inventory_digest",
            "source_inventory_revision",
            "last_checked_message_hash_ts",
            "last_checked_message_hashes",
        ):
            state.pop(key, None)
        state.pop("last_topic_key", None)
        state.pop("last_notified_ts", None)

    MonitorStateStore(path).update(mutate)


def state_file_for_chat(username, state_dir=STATE_DIR):
    """Return a stable per-chat monitor state path."""
    digest = hashlib.sha256(str(username or "").encode("utf-8")).hexdigest()[:16]
    return os.path.join(state_dir, f"{digest}.json")


class TopicMonitor:
    """Check one chat for user-interesting topics."""

    def __init__(
        self,
        db,
        config,
        state_file=STATE_FILE,
        hits_dir=HITS_DIR,
        ai_evaluator=None,
        relation_evaluator=None,
        knowledge_store=None,
        review_queue=None,
        link_preview_fetcher=None,
        state_store=None,
        now_func=time.time,
    ):
        self.db = db
        self.config = config
        self.state_file = state_file
        self.hits_dir = hits_dir
        self.ai_evaluator = ai_evaluator
        self.relation_evaluator = relation_evaluator
        self.knowledge_store = knowledge_store
        self.review_queue = review_queue
        # Retained as an inert constructor argument for caller compatibility.
        # The normal monitor has no remote-preview network path.
        self.link_preview_fetcher = None
        self.state_store = state_store or MonitorStateStore(state_file)
        self.now_func = now_func

    def check_once(self, dry_run=False):
        if dry_run:
            return self._check_once(dry_run=True)
        try:
            with self.state_store.execution_lock():
                return self._check_once(dry_run=False)
        except MonitorStateError as exc:
            return self._state_error_result(exc)

    def _check_once(self, dry_run=False):
        """Run a single monitor check.

        Args:
            dry_run: When True, do not update state or save hit files.
        """
        topic = self.config.get("monitor_topic", "").strip()
        if not topic:
            return {"status": "missing_topic", "message": "请先设置关注描述"}

        username = self.config.get("monitor_chat_username", "").strip()
        if not username:
            raise MonitorConfigError("监控群聊未配置")

        try:
            snapshot = self.state_store.read()
        except MonitorStateError as exc:
            return self._state_error_result(exc)
        state = dict(snapshot.data)
        source_cursor_mode = (
            not dry_run and supports_monitor_source_cursors(self.db)
        )
        if not dry_run and not snapshot.existed:
            checkpoint = self.now_func()
            initial_state = {"last_checked_ts": checkpoint}
            if source_cursor_mode:
                try:
                    initial_state.update(
                        initialize_monitor_source_state(self.db, checkpoint)
                    )
                except MonitorSourceError as source_exc:
                    return self._source_error_result(source_exc)
            try:
                initialized = self.state_store.initialize_if_absent(initial_state)
            except MonitorStateError as exc:
                return self._state_error_result(exc)
            if not initialized.existed:
                return {"status": "initialized", "message": "已从当前时间开始监控"}
            snapshot = initialized
            state = dict(snapshot.data)
        if (
            not dry_run
            and not state.get("last_checked_ts")
            and not state.get("source_cursors")
        ):
            return {
                "status": "monitor_state_missing_checkpoint",
                "message": "monitor_state_missing_checkpoint",
            }

        pending = None
        if not dry_run:
            try:
                pending = pending_monitor_source_batch(state, self._batch_policy_fingerprint())
            except MonitorSourceError as exc:
                return self._source_error_result(exc)
            if pending is not None and not source_cursor_mode:
                return {"status": "monitor_pending_source_unavailable",
                        "message": "monitor_pending_source_unavailable"}

        if not dry_run:
            backoff_result = self._ai_backoff_result(state)
            if backoff_result:
                return backoff_result

        since_ts = self._get_since_ts(state, dry_run)
        max_messages = max(1, int(
            self.config.get("monitor_max_messages_per_run", 200)
        ))
        source_batch = None
        if source_cursor_mode:
            try:
                if pending is not None:
                    verify_monitor_source_inventory(self.db, pending["inventory_digest"], 0)
                source_batch = read_monitor_source_batch(
                    self.db,
                    username,
                    state,
                    raw_limit=pending["raw_count"] if pending else max_messages,
                )
            except MonitorSourceError as exc:
                return self._source_error_result(exc)
            if pending is not None:
                try:
                    verify_pending_monitor_source_batch(pending, source_batch)
                except MonitorSourceError as exc:
                    return self._source_error_result(exc)
            messages = list(source_batch.visible_messages)
            if source_batch.raw_count == 0:
                return self._commit_monitor_result(
                    snapshot,
                    state,
                    {
                        "status": "no_messages",
                        "message": "没有新消息",
                        "raw_message_count": 0,
                        "source_eof": source_batch.source_eof,
                    },
                    source_batch,
                )
            if not messages:
                return self._commit_monitor_result(
                    snapshot,
                    state,
                    {
                        "status": "source_advanced_no_visible",
                        "message": "已推进源消息游标；本批没有可展示消息",
                        "message_count": 0,
                        "raw_message_count": source_batch.raw_count,
                        "source_eof": source_batch.source_eof,
                    },
                    source_batch,
                )
            recovered = self._recover_committed_source_batch(source_batch)
            if recovered is not None:
                if recovered.get("status") == "knowledge_recovery_unavailable":
                    return recovered
                return self._commit_monitor_result(
                    snapshot,
                    state,
                    recovered,
                    source_batch,
                )
            context_messages = self._source_cursor_context_messages(
                state,
                messages,
                max_messages,
            )
            if pending is None:
                # Publish intent before the provider runs. It changes no source
                # cursor and is acknowledged with the eventual cursor CAS.
                try:
                    state["pending_source_batch"] = freeze_monitor_source_batch(
                        source_batch, state, self._batch_policy_fingerprint(),
                    )
                    snapshot = self.state_store.commit(snapshot.revision, state)
                    state = dict(snapshot.data)
                except MonitorStateError as exc:
                    return self._state_error_result(exc)
        else:
            query_since_ts = self._get_query_since_ts(since_ts)
            page_forward = not dry_run and since_ts > 0
            processed_hashes = (
                self._processed_hashes_for_timestamp(state, since_ts)
                if page_forward else set()
            )
            if page_forward and processed_hashes:
                query_since_ts = min(query_since_ts, max(0, since_ts - 0.001))
            query_limit = max_messages + self._context_max_messages()
            if page_forward:
                query_limit += len(processed_hashes)
            scanned_messages = self.db.get_messages(
                username,
                since_ts=query_since_ts,
                limit=query_limit,
                page_forward=page_forward,
            )
            context_messages, messages = self._split_context_messages(
                scanned_messages,
                since_ts,
                max_messages,
                page_forward=page_forward,
                processed_message_hashes=processed_hashes,
            )
            if page_forward and not messages and query_since_ts < since_ts:
                # Compatibility for pre-cursor adapters and dry-run reads only.
                retry_since_ts = max(0, since_ts - 0.001)
                scanned_messages = self.db.get_messages(
                    username,
                    since_ts=retry_since_ts,
                    limit=max_messages + len(processed_hashes),
                    page_forward=page_forward,
                )
                context_messages, messages = self._split_context_messages(
                    scanned_messages,
                    since_ts,
                    max_messages,
                    page_forward=page_forward,
                    processed_message_hashes=processed_hashes,
                )
            if not messages:
                return {"status": "no_messages", "message": "没有新消息"}

        messages_text = redact_urls_in_text(self.db.format_messages_for_ai(
            messages,
            show_group_nickname=self.config.get("show_group_nickname", True),
        ))
        context_text = redact_urls_in_text(self.db.format_messages_for_ai(
            context_messages,
            show_group_nickname=self.config.get("show_group_nickname", True),
        )) if context_messages else ""
        source_messages = self._source_messages(context_messages, messages)
        source_messages_text = redact_urls_in_text(self.db.format_messages_for_ai(
            source_messages,
            show_group_nickname=self.config.get("show_group_nickname", True),
        ))
        try:
            decision = self._evaluate_with_retry(
                messages,
                messages_text,
                topic,
                context_text,
            )
        except MonitorConfigError:
            raise
        except Exception as exc:
            if source_batch is not None:
                try:
                    verify_monitor_source_inventory(
                        self.db,
                        source_batch.inventory_digest,
                        source_batch.inventory_revision,
                    )
                except MonitorSourceError as source_exc:
                    return self._source_error_result(source_exc)
            if not dry_run and (isinstance(exc, MonitorDecisionError) or is_retryable_ai_error(exc)):
                state_error = self._commit_retryable_ai_failure(snapshot, exc)
                if state_error:
                    return state_error
            if isinstance(exc, MonitorDecisionError):
                return {
                    "status": exc.code,
                    "message": "AI 返回格式无效；未推进消息游标，稍后重试",
                    "error_code": exc.code,
                }
            raise
        normalized = self._normalize_decision(decision, messages)
        if not dry_run:
            self._clear_ai_failure_state(state)

        last_msg_ts = messages[-1]["timestamp"]
        result = {
            "status": "no_match",
            "message_count": len(messages),
            "last_msg_ts": last_msg_ts,
            "decision": normalized,
        }
        if source_batch is not None:
            result.update({
                "raw_message_count": source_batch.raw_count,
                "source_eof": source_batch.source_eof,
            })

        if not dry_run and source_batch is None:
            self._advance_checkpoint_state(state, messages, last_msg_ts)

        if not normalized["match"] or normalized["score"] < 70:
            if not dry_run:
                return self._commit_monitor_result(
                    snapshot, state, result, source_batch
                )
            return result

        if self._knowledge_enabled():
            knowledge_result = self._process_with_knowledge(
                normalized,
                messages,
                source_messages_text,
                dry_run=dry_run,
                source_batch_id=(
                    source_batch.source_batch_id if source_batch is not None else ""
                ),
            )
            result.update(knowledge_result)
            if dry_run:
                result["status"] = "matched"
                return result

            state["last_topic_key"] = normalized["topic_key"]
            if result.get("status") == "duplicate":
                return self._commit_monitor_result(
                    snapshot, state, result, source_batch
                )

            if result.get("relation") in RELATION_NOTIFY:
                state["last_notified_ts"] = self.now_func()
                hit_path = self._save_hit(
                    source_messages,
                    normalized,
                    source_batch_id=(
                        source_batch.source_batch_id
                        if source_batch is not None
                        else ""
                    ),
                )
                result["hit_path"] = hit_path
            return self._commit_monitor_result(
                snapshot, state, result, source_batch
            )

        if self._is_in_cooldown(state, normalized):
            result["status"] = "cooldown"
            if not dry_run:
                return self._commit_monitor_result(
                    snapshot, state, result, source_batch
                )
            return result

        result.update({
            "status": "matched" if dry_run else "notified",
            "title": normalized["title"],
            "summary": normalized["summary"],
            "topic_key": normalized["topic_key"],
        })

        if not dry_run:
            hit_path = self._save_hit(
                source_messages,
                normalized,
                source_batch_id=(
                    source_batch.source_batch_id if source_batch is not None else ""
                ),
            )
            state["last_topic_key"] = normalized["topic_key"]
            state["last_notified_ts"] = self.now_func()
            result["hit_path"] = hit_path
            return self._commit_monitor_result(
                snapshot, state, result, source_batch
            )

        return result

    @staticmethod
    def _state_error_result(error):
        code = error.code if isinstance(error, MonitorStateError) else "monitor_state_error"
        return {"status": code, "message": code}

    @staticmethod
    def _source_error_result(error):
        code = (
            error.code
            if isinstance(error, MonitorSourceError)
            else "monitor_source_error"
        )
        result = {"status": code, "message": code}
        details = getattr(error, "details", None)
        if isinstance(details, dict):
            result.update(details)
        return result

    def _commit_monitor_result(self, snapshot, state, result, source_batch=None):
        if source_batch is not None:
            try:
                verify_monitor_source_inventory(
                    self.db,
                    source_batch.inventory_digest,
                    source_batch.inventory_revision,
                )
            except MonitorSourceError as exc:
                return self._source_error_result(exc)
            state_cursors = state.get("source_cursors")
            already_current = (
                source_batch.raw_count == 0
                and isinstance(state_cursors, dict)
                and state_cursors == source_batch.source_cursors
                and str(state.get("source_inventory_digest") or "")
                == source_batch.inventory_digest
                and "last_checked_message_hash_ts" not in state
                and "last_checked_message_hashes" not in state
            )
            if already_current:
                result.setdefault(
                    "source_inventory_digest", source_batch.inventory_digest
                )
                result.setdefault("source_eof", source_batch.source_eof)
                result.setdefault("raw_message_count", source_batch.raw_count)
                return result
            state.pop("pending_source_batch", None)
            state["source_cursors"] = {
                key: dict(value)
                for key, value in source_batch.source_cursors.items()
            }
            state["source_inventory_digest"] = source_batch.inventory_digest
            state["source_inventory_revision"] = source_batch.inventory_revision
            state["last_checked_ts"] = source_batch.last_checked_ts
            state.pop("last_checked_message_hash_ts", None)
            state.pop("last_checked_message_hashes", None)
            result.setdefault("source_inventory_digest", source_batch.inventory_digest)
            result.setdefault("source_eof", source_batch.source_eof)
            result.setdefault("raw_message_count", source_batch.raw_count)
        return self._commit_state_result(snapshot, state, result)

    def _commit_state_result(self, snapshot, state, result):
        try:
            self.state_store.commit(snapshot.revision, state)
        except MonitorStateError as exc:
            return self._state_error_result(exc)
        return result

    def _get_since_ts(self, state, dry_run):
        if dry_run:
            interval = self.config.get("monitor_interval_minutes", 3)
            return self.now_func() - interval * 60
        if state.get("last_checked_ts"):
            return float(state["last_checked_ts"])
        return 0

    def _source_cursor_context_messages(self, state, messages, raw_limit):
        checkpoint = self._get_since_ts(state, False)
        overlap_seconds = self._context_overlap_seconds()
        context_limit = self._context_max_messages()
        if checkpoint <= 0 or overlap_seconds <= 0 or context_limit <= 0:
            return []
        scanned = self.db.get_messages(
            self.config.get("monitor_chat_username", "").strip(),
            since_ts=max(0, checkpoint - overlap_seconds),
            limit=context_limit + max(1, int(raw_limit)),
            page_forward=False,
        )
        current_ids = {
            str(message.get("source_message_id") or "")
            for message in messages
        }
        context = [
            message
            for message in scanned
            if self._message_timestamp(message) <= checkpoint
            and str(message.get("source_message_id") or "") not in current_ids
        ]
        context.sort(key=lambda message: (
            self._message_timestamp(message),
            str(message.get("source_message_id") or ""),
        ))
        return context[-context_limit:]

    def _ai_backoff_result(self, state):
        try:
            retry_after = float(state.get("ai_next_retry_after") or 0)
        except (TypeError, ValueError):
            return None
        now = self.now_func()
        if retry_after <= now:
            return None
        remaining = max(1, int((retry_after - now + 59) // 60))
        return {
            "status": "ai_backoff",
            "message": f"AI API 暂时不可用，约 {remaining} 分钟后重试",
            "retry_after_ts": retry_after,
            "last_error_code": (
                state.get("ai_last_error_code") or "ai_retryable_error"
            ),
        }

    def _ai_failure_backoff_minutes(self):
        try:
            minutes = int(
                self.config.get("monitor_ai_failure_backoff_minutes", 10)
            )
        except (TypeError, ValueError):
            minutes = 10
        return max(1, min(1440, minutes))

    @staticmethod
    def _ai_failure_code(error):
        if isinstance(error, MonitorDecisionError):
            return error.code
        text = str(error or "").casefold()
        if "empty response" in text or "空响应" in text:
            return "ai_empty_response"
        if "timeout" in text or "timed out" in text or "请求超时" in text:
            return "ai_timeout"
        if (
            "connect" in text
            or "connection" in text
            or "无法连接" in text
        ):
            return "ai_connection_error"
        if "network" in text or "网络连接" in text:
            return "ai_network_error"
        return "ai_service_unavailable"

    def _commit_retryable_ai_failure(self, snapshot, error):
        """Persist only backoff metadata against the observed state revision."""
        failure_state = dict(snapshot.data)
        try:
            failure_count = int(failure_state.get("ai_failure_count") or 0)
        except (TypeError, ValueError):
            failure_count = 0
        now = float(self.now_func())
        failure_state.update({
            "ai_failure_count": failure_count + 1,
            "ai_last_error_code": self._ai_failure_code(error),
            "ai_last_error_ts": now,
            "ai_next_retry_after": (
                now + self._ai_failure_backoff_minutes() * 60
            ),
        })
        failure_state.pop("ai_last_error", None)
        try:
            self.state_store.commit(snapshot.revision, failure_state)
        except MonitorStateError as exc:
            return self._state_error_result(exc)
        return None

    def _ai_retry_attempts(self):
        try:
            attempts = int(self.config.get("monitor_ai_retry_attempts", 1))
        except (TypeError, ValueError):
            attempts = 1
        return max(0, min(3, attempts))

    def _ai_retry_delay_seconds(self):
        try:
            delay = int(self.config.get("monitor_ai_retry_delay_seconds", 3))
        except (TypeError, ValueError):
            delay = 3
        return max(0, min(60, delay))

    @staticmethod
    def _clear_ai_failure_state(state):
        for key in (
            "ai_failure_count",
            "ai_last_error",
            "ai_last_error_code",
            "ai_last_error_ts",
            "ai_next_retry_after",
        ):
            state.pop(key, None)

    def _get_query_since_ts(self, since_ts):
        overlap_seconds = self._context_overlap_seconds()
        if since_ts <= 0 or overlap_seconds <= 0:
            return since_ts
        return max(0, since_ts - overlap_seconds)

    def _context_overlap_seconds(self):
        try:
            minutes = int(self.config.get("monitor_context_overlap_minutes", 12))
        except (TypeError, ValueError):
            minutes = 12
        return max(0, minutes) * 60

    def _context_max_messages(self):
        try:
            limit = int(self.config.get("monitor_context_max_messages", 80))
        except (TypeError, ValueError):
            limit = 80
        return max(0, limit)

    def _split_context_messages(
        self,
        scanned_messages,
        since_ts,
        max_messages,
        page_forward=False,
        processed_message_hashes=None,
    ):
        processed_message_hashes = set(processed_message_hashes or [])
        if since_ts <= 0 or self._context_overlap_seconds() <= 0:
            if page_forward and processed_message_hashes:
                scanned_messages = [
                    msg for msg in scanned_messages
                    if self._message_timestamp(msg) > since_ts
                    or self._message_hash(msg) not in processed_message_hashes
                ]
            messages = (
                scanned_messages[:max_messages]
                if page_forward else scanned_messages[-max_messages:]
            )
            return [], messages

        context_limit = self._context_max_messages()
        context_messages = []
        messages = []
        for msg in scanned_messages:
            timestamp = self._message_timestamp(msg)
            if (
                page_forward
                and timestamp == since_ts
                and self._message_hash(msg) in processed_message_hashes
            ):
                continue
            if timestamp <= since_ts and not (
                page_forward and timestamp == since_ts
            ):
                context_messages.append(msg)
            else:
                messages.append(msg)
        if context_limit:
            context_messages = context_messages[-context_limit:]
        else:
            context_messages = []
        messages = (
            messages[:max_messages]
            if page_forward else messages[-max_messages:]
        )
        return context_messages, messages

    @staticmethod
    def _message_timestamp(message):
        return float(message.get("timestamp", 0) or 0)

    @staticmethod
    def _message_hash(message):
        return build_message_hash([message])

    def _processed_hashes_for_timestamp(self, state, timestamp):
        try:
            hash_ts = float(state.get("last_checked_message_hash_ts", -1))
        except (TypeError, ValueError):
            return set()
        if hash_ts != float(timestamp):
            return set()
        hashes = state.get("last_checked_message_hashes") or []
        if not isinstance(hashes, list):
            return set()
        return {str(item) for item in hashes if item}

    def _advance_checkpoint_state(self, state, messages, last_msg_ts):
        hashes = [
            self._message_hash(msg)
            for msg in messages
            if self._message_timestamp(msg) == float(last_msg_ts)
        ]
        if float(state.get("last_checked_ts", -1) or -1) == float(last_msg_ts):
            hashes = list(dict.fromkeys([
                *self._processed_hashes_for_timestamp(state, last_msg_ts),
                *hashes,
            ]))
        state["last_checked_ts"] = last_msg_ts
        state["last_checked_message_hash_ts"] = last_msg_ts
        state["last_checked_message_hashes"] = hashes

    @staticmethod
    def _source_messages(context_messages, messages):
        if not context_messages:
            return messages
        combined = context_messages + messages
        combined.sort(key=lambda msg: float(msg.get("timestamp", 0) or 0))
        return combined

    def _knowledge_enabled(self):
        return bool(self.config.get("monitor_knowledge_enabled", False))

    def _get_knowledge_store(self, dry_run=False):
        if self.knowledge_store is not None:
            return self.knowledge_store
        return KnowledgeStore.from_config(self.config, now_func=self.now_func, read_only=dry_run)

    def _recover_committed_source_batch(self, source_batch):
        """Advance a committed batch without asking the provider a second time."""
        if not self._knowledge_enabled() or not source_batch.source_batch_id:
            return None
        store = self._get_knowledge_store(dry_run=False)
        try:
            knowledge = store.recover_source_batch(source_batch.source_batch_id)
        except Exception as exc:
            return {
                "status": "knowledge_recovery_unavailable",
                "message": "knowledge_recovery_unavailable",
                "error_code": type(exc).__name__,
            }
        if knowledge is None:
            return None
        source_window = {
            "start": str(knowledge.get("window_start") or ""),
            "end": str(knowledge.get("window_end") or ""),
        }
        return {
            "status": "duplicate",
            "message": "canonical source batch already committed",
            "message_count": len(source_batch.visible_messages),
            "raw_message_count": source_batch.raw_count,
            "source_eof": source_batch.source_eof,
            "knowledge_topic_id": knowledge.get("topic_id"),
            "knowledge_event_id": knowledge.get("event_id"),
            "knowledge_event_written": False,
            "knowledge_event_reused": True,
            "knowledge_path": knowledge.get("knowledge_path", ""),
            "obsidian_path": knowledge.get("obsidian_path", ""),
            "knowledge_projection_warnings": knowledge.get(
                "projection_warnings", []
            ),
            "source_window": source_window,
            "affected_dates": source_window_dates(
                self.config,
                source_window["start"],
                source_window["end"],
                fallback_ts=knowledge.get("event_created_at"),
            ),
        }

    def _get_review_queue(self):
        if self.review_queue is not None:
            return self.review_queue
        return ReviewQueue.from_config(self.config, now_func=self.now_func)

    def _process_with_knowledge(
        self,
        decision,
        messages,
        messages_text,
        dry_run=False,
        source_batch_id="",
    ):
        candidate = normalize_candidate(decision)
        candidate["source_chat"] = self.config.get("monitor_chat_display_name", "监控群聊")
        candidate["source_chat_username"] = str(self.config.get("monitor_chat_username") or "").strip()
        candidate["vault_chat_name"] = vault_chat_name(self.config)
        candidate["message_hash"] = build_message_hash(messages)
        if messages:
            candidate["window_start"] = messages[0].get("time_str", "")
            candidate["window_end"] = messages[-1].get("time_str", "")
        store = self._get_knowledge_store(dry_run=dry_run)
        relation_lookup_error = ""
        try:
            exact_topic_id = store.topic_id_for_message_hash(
                candidate["message_hash"],
                source_chat_username=candidate["source_chat_username"],
                source_chat=candidate["source_chat"],
            )
        except Exception as exc:
            exact_topic_id = None
            relation_lookup_error = type(exc).__name__
        candidate["existing_message_topic_id"] = exact_topic_id
        candidates = store.find_candidates(candidate)
        candidate_ids = {item.get("topic_id") for item in candidates}
        if exact_topic_id and exact_topic_id not in candidate_ids:
            exact_topic = store.get_topic(exact_topic_id)
            if exact_topic:
                exact_topic["score"] = 100
                candidates.insert(0, exact_topic)
        relation_decision = self._classify_knowledge_relation(candidate, candidates, messages_text)
        if relation_lookup_error:
            relation_decision = {
                "relation": "new",
                "target_topic_id": None,
                "reason": "exact message lookup failed; conservatively treating as new",
                "source": "insufficient_relation_evidence",
            }
        relation = normalize_relation(relation_decision.get("relation"))

        if dry_run:
            result = {
                "relation": relation,
                "relation_reason": relation_decision.get("reason", ""),
                "relation_source": relation_decision.get("source", ""),
                "title": self._notification_title(decision, relation),
                "summary": decision["summary"],
                "topic_key": decision["topic_key"],
                "knowledge_candidates": candidates,
                "knowledge_candidate": candidate,
            }
            if relation_lookup_error:
                result["relation_lookup_error"] = relation_lookup_error
            return result

        if relation != "new" and not relation_decision.get("target_topic_id"):
            if candidates:
                relation_decision["target_topic_id"] = candidates[0]["topic_id"]
            else:
                relation_decision["relation"] = "new"
                relation = "new"

        if relation == "new":
            related_ids = self._strong_related_topic_ids(candidate, candidates)
            if related_ids:
                relation_decision["related_topic_ids"] = related_ids

        status = "duplicate" if relation == "duplicate" else "notified"
        if source_batch_id:
            knowledge = store.apply_event(
                candidate,
                messages,
                self.config,
                relation_decision,
                source_batch_id=source_batch_id,
            )
        else:
            knowledge = store.apply_event(
                candidate,
                messages,
                self.config,
                relation_decision,
            )
        knowledge_reused = bool(knowledge.get("reused"))
        if knowledge_reused:
            status = "duplicate"
            relation = normalize_relation(knowledge.get("relation") or relation)
        source_window = {
            "start": candidate.get("window_start", ""),
            "end": candidate.get("window_end", ""),
        }
        fallback_ts = messages[-1].get("timestamp") if messages else None
        result = {
            "status": status,
            "relation": relation,
            "relation_reason": relation_decision.get("reason", ""),
            "relation_source": relation_decision.get("source", ""),
            "title": self._notification_title(decision, relation),
            "summary": decision["summary"],
            "topic_key": decision["topic_key"],
            "knowledge_topic_id": knowledge.get("topic_id"),
            "knowledge_event_id": knowledge.get("event_id"),
            "knowledge_event_written": (
                knowledge.get("event_id") is not None and not knowledge_reused
            ),
            "knowledge_event_reused": knowledge_reused,
            "source_window": source_window,
            "affected_dates": source_window_dates(
                self.config,
                source_window["start"],
                source_window["end"],
                fallback_ts=fallback_ts,
            ),
            "knowledge_path": knowledge.get("knowledge_path", ""),
            "obsidian_path": knowledge.get("obsidian_path", ""),
            "knowledge_projection_warnings": knowledge.get("projection_warnings", []),
            "knowledge_candidates": candidates,
        }
        if relation_lookup_error:
            result["relation_lookup_error"] = relation_lookup_error
        if status == "notified":
            self._attach_review_queue_item(result, candidate, messages)
        return result

    def _review_queue_data(self, result, candidate, messages):
        files = extract_file_refs(messages, self.config)
        links = candidate.get("links") or []
        resource_status = self._resource_status_for_queue(candidate, files, links)
        resource_lead = bool(candidate.get("resource_lead")) or resource_status in {
            "mentioned_private",
            "mentioned_pending",
        }
        return {
            "source_chat": self.config.get("monitor_chat_display_name", "监控群聊"),
            "window_start": messages[0].get("time_str", "") if messages else "",
            "window_end": messages[-1].get("time_str", "") if messages else "",
            "title": result.get("title") or candidate.get("title"),
            "summary": result.get("summary") or candidate.get("summary"),
            "knowledge_topic_id": result.get("knowledge_topic_id"),
            "knowledge_event_id": result.get("knowledge_event_id"),
            "obsidian_path": result.get("obsidian_path", ""),
            "resource_lead": resource_lead,
            "resource_status": resource_status,
            "lead_key": (candidate.get("lead_key") or candidate.get("topic_key") or "") if resource_lead else "",
            "resources": {
                "files": files,
                "links": links,
            },
            "message_hash": build_message_hash(messages),
        }

    def _resource_status_for_queue(self, candidate, files, _links):
        status = self._normalize_resource_status(candidate.get("resource_status"))
        if files:
            return "attached"
        if status != "none":
            return status
        if candidate.get("resource_lead"):
            return "mentioned_pending"
        return "none"

    def _attach_review_queue_item(self, result, candidate, messages):
        try:
            queue = self._get_review_queue()
            preview_item = queue.build_item(self._review_queue_data(result, candidate, messages))
            result["review_priority"] = preview_item["priority"]
            result["suggested_action"] = preview_item["suggested_action"]
            result["signal_level"] = preview_item.get("signal_level", "")
            result["actionability"] = preview_item.get("actionability", "none")
            result["queue_worthy"] = bool(preview_item.get("queue_worthy"))
            result["notify_now"] = preview_item["priority"] in {"P1", "P2"}
            if not result["queue_worthy"]:
                return
            item = queue.create_or_reuse(preview_item)
            result["review_queue_item"] = item
        except Exception as exc:
            result["review_queue_error"] = str(exc)
            result["notify_now"] = True

    def _classify_knowledge_relation(self, candidate, candidates, messages_text):
        deterministic = self._deterministic_relation(candidate, candidates)
        if not self.relation_evaluator:
            return deterministic

        prompt = self._build_relation_prompt(candidate, candidates, messages_text)
        try:
            raw = self.relation_evaluator(prompt, self.config)
            decision = self._parse_json(raw) if isinstance(raw, str) else raw
        except Exception:
            return deterministic

        if not isinstance(decision, dict):
            return deterministic

        raw_relation = str(decision.get("relation") or "").strip().lower()
        if raw_relation not in {"duplicate", "update", "new", "contradiction"}:
            return deterministic

        target_topic_id = decision.get("target_topic_id")
        candidate_ids = {c["topic_id"] for c in candidates}
        try:
            target_topic_id = int(target_topic_id)
        except (TypeError, ValueError):
            target_topic_id = None
        relation = normalize_relation(raw_relation)
        if relation != "new" and target_topic_id not in candidate_ids:
            return deterministic
        if relation == "new":
            target_topic_id = None

        return {
            "relation": relation,
            "target_topic_id": target_topic_id,
            "reason": str(decision.get("reason") or "").strip(),
            "source": "injected_evaluator",
        }

    @staticmethod
    def _stable_chat_key(item):
        username = str(item.get("source_chat_username") or "").strip().casefold()
        if username:
            return f"username:{username}"
        name = str(item.get("vault_chat_name") or item.get("source_chat") or "").strip().casefold()
        return f"name:{name}" if name else ""

    @classmethod
    def _same_stable_chat(cls, candidate, topic):
        candidate_username = str(candidate.get("source_chat_username") or "").strip().casefold()
        topic_username = str(topic.get("source_chat_username") or "").strip().casefold()
        if candidate_username and topic_username:
            return candidate_username == topic_username
        candidate_name = str(
            candidate.get("vault_chat_name") or candidate.get("source_chat") or ""
        ).strip().casefold()
        topic_name = str(topic.get("vault_chat_name") or topic.get("source_chat") or "").strip().casefold()
        return bool(candidate_name and topic_name and candidate_name == topic_name)

    @staticmethod
    def _shared_links(candidate, topic):
        candidate_links = {str(link).strip().casefold() for link in candidate.get("links") or [] if str(link).strip()}
        topic_links = {str(link).strip().casefold() for link in topic.get("links") or [] if str(link).strip()}
        return candidate_links & topic_links

    @staticmethod
    def _is_history_topic(topic):
        topic_key = str(topic.get("topic_key") or "")
        title = str(topic.get("title") or "")
        return topic_key.startswith("history-summary:") or "历史总结" in title

    @staticmethod
    def _is_disputed_candidate(candidate):
        status = str(candidate.get("status_hint") or "").strip().casefold()
        event_type = str(candidate.get("event_type") or "").strip().casefold()
        return status in {"disputed", "debunked", "denied", "false"} or event_type in {
            "contradiction",
            "correction",
            "debunk",
        }

    def _deterministic_relation(self, candidate, candidates):
        if not candidates:
            return {
                "relation": "new",
                "target_topic_id": None,
                "reason": "deterministic: no similar existing topic",
                "source": "no_candidates",
            }

        exact_topic_id = candidate.get("existing_message_topic_id")
        if exact_topic_id:
            return {
                "relation": "duplicate",
                "target_topic_id": int(exact_topic_id),
                "reason": "deterministic: exact message window already recorded",
                "source": "exact_message_hash",
            }

        candidate_topic_key = str(candidate.get("topic_key") or "").strip().casefold()
        for topic in candidates:
            if not self._same_stable_chat(candidate, topic) or self._is_history_topic(topic):
                continue
            topic_key = str(topic.get("topic_key") or "").strip().casefold()
            exact_topic_key = bool(candidate_topic_key and candidate_topic_key == topic_key)
            shared_links = self._shared_links(candidate, topic)
            if not exact_topic_key and not shared_links:
                continue
            relation = "contradiction" if self._is_disputed_candidate(candidate) else "update"
            evidence = "exact topic key" if exact_topic_key else "shared link"
            source = (
                "disputed_same_topic"
                if relation == "contradiction"
                else ("same_topic_key" if exact_topic_key else "shared_link")
            )
            return {
                "relation": relation,
                "target_topic_id": int(topic["topic_id"]),
                "reason": f"deterministic: {evidence} in the same chat",
                "source": source,
            }

        return {
            "relation": "new",
            "target_topic_id": None,
            "reason": "deterministic: similarity lacks same-chat continuation evidence",
            "source": "insufficient_relation_evidence",
        }

    @staticmethod
    def _relation_entity_keys(items):
        weak = {
            "ai",
            "claude",
            "codex",
            "openai",
            "deepseek",
            "模型",
            "群友",
            "工具",
        }
        return {
            str(item).strip().casefold()
            for item in items or []
            if str(item).strip() and str(item).strip().casefold() not in weak
        }

    def _has_strong_related_evidence(self, candidate, topic):
        candidate_topic_key = str(candidate.get("topic_key") or "").strip().casefold()
        topic_key = str(topic.get("topic_key") or "").strip().casefold()
        if candidate_topic_key and candidate_topic_key == topic_key:
            return True
        if self._shared_links(candidate, topic):
            return True
        shared_entities = self._relation_entity_keys(candidate.get("entities")) & self._relation_entity_keys(
            topic.get("entities")
        )
        if not shared_entities:
            return False
        candidate_tokens = KnowledgeStore._title_tokens(candidate.get("title"))
        topic_tokens = KnowledgeStore._title_tokens(topic.get("title"))
        return len(candidate_tokens & topic_tokens) >= 2

    def _strong_related_topic_ids(self, candidate, candidates):
        related_ids = []
        for topic in candidates:
            if self._is_history_topic(topic) or not self._same_stable_chat(candidate, topic):
                continue
            if float(topic.get("score") or 0) < 80:
                continue
            if not self._has_strong_related_evidence(candidate, topic):
                continue
            topic_id = int(topic["topic_id"])
            if topic_id not in related_ids:
                related_ids.append(topic_id)
            if len(related_ids) == 3:
                break
        return related_ids

    def _build_relation_prompt(self, candidate, candidates, messages_text):
        candidate_text = redact_urls_in_text(
            json.dumps(candidate, ensure_ascii=False, indent=2)
        )
        candidates_text = redact_urls_in_text(
            json.dumps(candidates, ensure_ascii=False, indent=2)
        )
        messages_text = redact_urls_in_text(messages_text)
        return f"""你是微信群关注推送的本地知识库判重助手。请判断这次新命中的候选内容与已有主题的关系。

关系只能是：
- duplicate：只是旧主题的重复说法，没有新增事实、链接、结论、反转或重要人物回应。
- update：属于已有主题的新线索、新链接、新事实、新测评、新讨论进展，值得提醒。
- contradiction：对已有主题形成辟谣、反转、纠错、冲突证据，值得提醒。
- new：不是这些旧主题，应该新建主题。

判断标准要适配微信群消息：
- 同一个传闻/发布/链接/模型测评，短时间内可能多次刷屏；没有新信息就是 duplicate。
- 同主题里出现新链接、更多人确认、关键说法变化、官方/半官方来源、实际测评、发布时间线推进，就是 update。
- 旧传闻被否认、图片被指出是假的、结论相反，就是 contradiction。
- 只按语义和事实判断，不要因为标题不同就判 new。

<new_candidate>
{candidate_text}
</new_candidate>

<existing_topics>
{candidates_text}
</existing_topics>

<source_messages>
{messages_text}
</source_messages>

只输出严格 JSON：
{{
  "relation": "duplicate|update|new|contradiction",
  "target_topic_id": 123,
  "reason": "一句话解释"
}}"""

    @staticmethod
    def _notification_title(decision, relation):
        if relation == "update":
            return f"新线索: {decision['title']}"
        if relation == "contradiction":
            return f"反转/辟谣: {decision['title']}"
        return decision["title"]

    def _evaluate(self, messages, messages_text, topic, context_text=""):
        prompt = self._build_prompt(messages, messages_text, topic, context_text)
        if self.ai_evaluator:
            return self.ai_evaluator(prompt, self.config)
        return self._call_ai_provider(prompt)

    def _evaluate_with_retry(self, messages, messages_text, topic, context_text=""):
        attempts = self._ai_retry_attempts() + 1
        delay = self._ai_retry_delay_seconds()
        for attempt in range(1, attempts + 1):
            try:
                return self._validate_decision(
                    self._evaluate(messages, messages_text, topic, context_text)
                )
            except MonitorConfigError:
                raise
            except Exception as e:
                if attempt >= attempts or not (
                    isinstance(e, MonitorDecisionError) or is_retryable_ai_error(e)
                ):
                    safe_error = redact_urls_in_text(e)
                    if safe_error != str(e):
                        raise RuntimeError(safe_error) from None
                    raise
                print(
                    "[monitor] AI 调用失败，准备短重试 "
                    f"{attempt}/{attempts - 1}: {redact_urls_in_text(e)}"
                )
                if delay:
                    time.sleep(delay)

    def _build_prompt(self, messages, messages_text, topic, context_text=""):
        group_name = redact_urls_in_text(
            self.config.get("monitor_chat_display_name", "监控群聊")
        )
        topic = redact_urls_in_text(topic)
        messages_text = redact_urls_in_text(messages_text)
        context_text = redact_urls_in_text(context_text)
        start_time = messages[0].get("time_str", "")
        end_time = messages[-1].get("time_str", "")
        taxonomy_context = self._build_taxonomy_context()
        return f"""你是一个专业的微信群消息关注与总结助手。请根据用户自己设定的关注描述，判断新增消息中是否有值得提醒用户查看的内容；如果值得提醒，就生成一段简洁、有用、按话题整理的小摘要。

不要把关注描述当作关键词搜索。先理解用户真正想捕捉的信息类型，再结合群聊上下文判断是否语义相关、是否有具体内容、是否值得现在提醒。用户的关注点可能来自工作、AI、家人、朋友、生活安排、兴趣娱乐或任何其他场景；判断标准以用户写下的关注描述为准。

<user_interest>
{topic}
</user_interest>

<chat_context>
群聊：{group_name}
时间：{start_time} ~ {end_time}
消息数：{len(messages)}
</chat_context>

<recent_context>
{context_text or "无"}
</recent_context>

<taxonomy_context>
{taxonomy_context}
</taxonomy_context>

<decision_policy>
1. 先理解用户关注描述背后的真实意图，包括用户想知道的对象、事件、变化、机会、风险、情绪或提醒。
2. 从新增消息中聚合 0-3 个候选话题；不要逐条消息机械判断，也不要只按关键词判断。
3. 对每个候选话题按以下维度估分：
   - semantic_relevance：是否真的符合用户兴趣，而不是只出现相似词。
   - usefulness：是否满足用户描述的价值类型；可能是事实信息、提醒、风险、决策价值、情绪价值、趣味性、关系动态或启发。
   - novelty：是否像新增信息，而不是重复闲聊或已知内容。
   - urgency：是否值得现在通知用户，而不是等之后总结也行。
4. 如果用户关注的是轻松内容、情绪动态、家庭消息、朋友近况、趣事或八卦，不要用“工作价值/行动价值”过滤它；改用“是否符合用户想看的东西、是否有看点、是否有人回应、是否可能影响用户”来判断。
5. 如果用户关注的是宽口径主题（例如新鲜事、新想法、重要更新、家人近况、好玩的事），请从新增消息中提炼亮点，而不是等待明确公告。
6. 只有综合判断“符合用户关注描述且值得提醒”时 match=true。
7. 宁可漏掉无看点的水聊，也不要频繁误报。
8. 不要被闲聊比例稀释：即使 100 条里只有 1-2 条有价值，只要那 1-2 条明确符合用户关注描述，也要作为候选判断。
9. 如果用户关注描述包含新功能、产品更新、AI 工具、链接、教程、实验报告、具体做法、自建 app 或 agent 设计，那么“明确对象 + 明确变化/功能/做法/链接/结论”的单条消息也可以通知；不要因为只有单条就降到 70 分以下。
10. 如果同一时间窗里有多个候选都达到通知门槛，不要只保留最热门、最严肃或最后出现的一条；digest 可以同时列出多个话题，title 和 topic_key 用能覆盖这些话题的稳定短语。
11. 如果消息讨论的是 AI/agent/模型互动实验、可玩玩法、角色/场景测试、模型行为边界或偏好反馈，即使语气轻松、带玩笑或关系向表达，也要先按大类判断是否有方法、结果或启发；不要按单个敏感词字面过滤或命中。
12. Remote link preview 已禁用；消息中的 URL 只能作为未读取的引用，不要臆造网页正文。URL credential 会在发送给模型前替换为 REDACTED。
13. recent_context 是上一轮已经检查过的少量前文，只用来理解新增消息里的“这个/这样/对呀/role 不对”等省略、指代和断续讨论。不要因为 recent_context 自己有价值就通知；只有新增消息延续、补充、纠错或形成结论时，才把前文和新增内容合并成一个完整话题。
14. 不要把亲密关系、身体体验、情感陪伴或 AI 伴侣交互内容自动当作低价值或需要过滤；如果它们处在 skill、prompt、interaction design、agent/system design、资源分享、模型行为观察或人机关系设计语境下，按正常资源/设计/实验信息判断。
15. 识别 resource lead：如果有人表示自己有/做了/愿意发/可私发某个资源，群友开始索要，但当前消息窗口还没有实际文件或链接，例如“可以私发吗”“求一份/伸手/发我”“晚点发/回头发/不方便公开”“repo 还没公开”，应标记 resource_lead=true，resource_status=mentioned_private 或 mentioned_pending。
16. 提供商活动、优惠、额度、模型动态也值得 retain；如果只是“qoder 每天免费调用”“Grok 三个月优惠”“opencode 可调某模型”这类观察或机会，通常写入 note/daily digest 即可，不要为了有链接就标成即时资源线索，除非它同时是 repo/source/package/deploy 候选、存在安全/账号风险，或需要用户马上回群索要。
</decision_policy>

<negative_rules>
以下情况不要通知：
- 只是提到相关词，但没有实质内容。
- 与用户兴趣无关的玩笑、表情、复读、寒暄、跑题闲聊。
- 和兴趣点只弱相关，用户之后看总结也不迟。
- 没有明确证据消息支撑。
- 只有单句暧昧暗示，缺少上下文，无法判断价值。
- 纯玩笑或纯成人向闲聊，且看不出 AI/agent/模型互动实验、模型行为观察、方法或结果反馈。
- 只有 recent_context 相关，但新增消息没有新事实、新结论、新链接、新做法或明确回应。
注意：如果单句里已经包含明确产品/项目/模型/工具名，加上新功能、更新、链接、教程、实验结果、修复方案或可执行做法，它不是“无上下文的新消息”，可以通知。
注意：如果用户关注描述本身就是轻松、情绪、关系、家庭或生活类内容，相关的玩笑、趣事、近况、反应和情绪变化可以通知；不要因为它不是严肃信息就判为无价值。
注意：如果亲密关系、身体体验、情感陪伴或 AI 伴侣交互词汇是在 skill/resource/design/interaction/system context 下出现，不要把它按“纯成人向闲聊”处理。
</negative_rules>

<scoring>
score 是 0-100：
- 0-39：无关或只有字面擦边。
- 40-59：有点相关，但不值得通知。
- 60-69：可能相关，但证据不足或价值一般，不通知。
- 70-84：明确相关，且有信息价值/启发价值/行动价值/情绪价值之一，可以通知。
- 85-100：高度相关、新颖、有明显看点或多人回应，值得立刻看。
对于宽口径兴趣：只要候选话题有明确上下文、有人回应、且符合用户关注描述中的价值类型，score 可以达到 70 以上。
对于新功能/产品更新/链接资源/实验报告/具体做法：只要有明确对象和可复查的信息，哪怕消息很短，也应给到 70-84。
低于 70 必须 match=false。
</scoring>

<output_rules>
只输出严格 JSON，不要 Markdown，不要解释 JSON 外的文字。
score 只用于程序内部判断，不要把评分写进 title 或 digest。
digest 是给用户看的最终内容，不要粘贴原文证据，不要输出“评分/证据/原文”栏目。
digest 使用群聊总结风格，按时间顺序列出 1-5 条，格式类似：
1. 【时间】A 提到了某个有用网站/链接：说明它能做什么，并保留网址
2. 【时间】B 提到某个新消息/安排/变化，C、D 有附和或补充
3. 【时间】C 提出某个观点，D 反驳了什么，最后大致形成什么结论
每条都要写清谁说了什么、为什么值得看；有链接、时间、决定、结论时必须保留。
如果只有一个话题，也写成 1 条；如果同一时间窗里有多个值得提醒的话题，digest 分条保留，不要为了单一 title 丢弃次要但符合关注描述的话题；如果没有值得提醒的内容，digest 为空字符串。
topic_key 用稳定短语，便于同一主题冷却去重。
category 默认用简短中文分类，例如 AI模型、工具更新、发布传闻、教程资源、群内八卦、生活安排、待确认信息、已辟谣、未分类；如果 taxonomy_context 给出固定分类，category 必须从固定分类中选择。
semantic_tags 是 0-8 个更细的自由标签，用来表达文件夹无法承载的交叉语义，例如 共读、玩具、长期记忆、模型偏好、风险边界；不要把 semantic_tags 当成新的文件夹分类。
entities 是涉及的人、产品、模型、公司、项目或群友昵称。
key_facts 是可复用到知识库的关键事实，不要写空泛评价。
links 必须提取消息中出现的 URL；没有则空数组。
event_type 用简短短语描述事件类型，例如 rumor、release、benchmark、resource、debunk、discussion。
status_hint 可为 tracking、rumor、confirmed、disputed、resolved；不确定用 tracking。
resource_lead 是布尔值：仅当存在“资源尚未到手但可追问/私聊/回群索要”的行动窗口时为 true。
resource_status 只能是 attached、linked、mentioned_private、mentioned_pending、none：文件已出现用 attached；明确 repo/link/source/package/deploy artifact 已出现用 linked；有人愿意私发/不方便公开用 mentioned_private；晚点发/未公开/待补资源用 mentioned_pending；普通讨论、provider 活动或概念信息用 none。
lead_key 用稳定短语描述这条资源线索，便于去重；没有 resource lead 时可为空或沿用 topic_key。
</output_rules>

<output_json>
{{
  "match": false,
  "score": 0,
  "reason": "一句话说明为什么通知或不通知",
  "title": "短标题",
  "digest": "1. 【时间】谁提到了什么，为什么值得看\\n2. 【时间】谁补充/反驳/附和了什么，结论是什么",
  "topic_key": "短主题标识",
  "category": "分类",
  "semantic_tags": ["共读", "长期记忆"],
  "entities": ["Claude", "OpenAI"],
  "key_facts": ["可沉淀的事实或线索"],
  "links": ["https://example.com"],
  "event_type": "rumor",
  "status_hint": "tracking",
  "resource_lead": false,
  "resource_status": "none",
  "lead_key": "短资源线索标识"
}}
</output_json>

<messages>
{messages_text}
</messages>"""

    def _build_taxonomy_context(self):
        resolution = resolve_taxonomy_profile(
            self.config,
            set(TAXONOMY_PROFILES),
            source_chat_username=self.config.get("monitor_chat_username", ""),
            source_chat=self.config.get("monitor_chat_display_name", ""),
            vault_chat_name=vault_chat_name(self.config),
        )
        if resolution.profile == HUMAN_AI_INTIMACY_PROFILE:
            categories = "\n".join(f"- {name}" for name in HUMAN_AI_INTIMACY_CATEGORIES)
            return (
                f"profile: {HUMAN_AI_INTIMACY_PROFILE}\n"
                "category 必须从以下固定分类中选择，不能创造新的 folder category：\n"
                f"{categories}\n"
                "semantic_tags 可补充交叉语义，例如 共读、玩具、长期记忆、角色卡、模型行为、资源私发、边界风险。"
            )
        return "无固定 taxonomy；category 可按用户兴趣使用简短中文分类，semantic_tags 可补充交叉语义。"

    def _call_ai_provider(self, prompt):
        """Classify monitor windows with the user-selected AI provider."""
        provider_config = dict(self.config)
        monitor_provider = str(provider_config.get("monitor_ai_provider") or "").strip()
        monitor_model = str(provider_config.get("monitor_ai_model") or "").strip()
        if monitor_provider:
            provider_config["ai_provider"] = monitor_provider
        if monitor_model:
            provider_config["ai_model"] = monitor_model
        # The monitor needs a short strict-JSON classification, not a long
        # reasoning trace. DeepSeek V4 defaults to thinking mode, where the
        # reasoning budget can finish without any final content on dense chat
        # windows. Disable it for this structured checkpoint-advancing path.
        provider_config["ai_thinking"] = bool(
            provider_config.get("monitor_ai_thinking", False)
        )

        try:
            from ai.factory import create_provider
            if provider_config.get("ai_provider") in {"custom", "qwen"}:
                provider_config["ai_timeout_seconds"] = provider_config.get(
                    "monitor_ai_timeout_seconds",
                    90,
                )
            return create_provider(provider_config).summarize(prompt)
        except ValueError as e:
            raise MonitorConfigError(str(e)) from None
        except Exception as e:
            provider = provider_config.get("ai_provider", "AI")
            raise RuntimeError(
                redact_urls_in_text(normalize_ai_error(e, provider))
            ) from None

    def _batch_policy_fingerprint(self):
        # Provider/model changes are allowed for recovery. Changing interest,
        # chat or canonical destination must not silently reinterpret an intent.
        basis = {
            "chat": str(self.config.get("monitor_chat_username") or "").strip(),
            "interest": str(self.config.get("monitor_topic") or "").strip(),
            "knowledge": self._knowledge_enabled(),
            "knowledge_db": os.path.abspath(os.path.expanduser(
                self.config.get("monitor_knowledge_db") or KNOWLEDGE_DB
            )) if self._knowledge_enabled() else "",
        }
        return hashlib.sha256(json.dumps(
            basis, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_decision(value):
        """Admit an explicit decision, never coerce corruption into a negative.

        The legacy summary and items body forms remain supported. A standalone
        JSON code fence is harmless; prose containing a JSON object is not an
        accepted response. Diagnostics deliberately contain no provider text.
        """
        def unique_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise MonitorDecisionError()
                result[key] = item
            return result

        def reject_constant(_value):
            raise MonitorDecisionError()

        if isinstance(value, str):
            text = value.strip()
            fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.S)
            if fenced:
                text = fenced.group(1)
            try:
                value = json.loads(
                    text, object_pairs_hook=unique_object,
                    parse_constant=reject_constant,
                )
            except (ValueError, TypeError, RecursionError):
                raise MonitorDecisionError() from None
        if (
            not isinstance(value, dict)
            or type(value.get("match")) is not bool
            or type(value.get("score")) is not int
            or not 0 <= value["score"] <= 100
            or (value["match"] and value["score"] < 70)
        ):
            raise MonitorDecisionError()
        for key in (
            "title", "digest", "summary", "topic_key", "category", "reason",
            "event_type", "status_hint", "resource_status", "lead_key",
        ):
            if key in value and not isinstance(value[key], str):
                raise MonitorDecisionError()
        for key in ("entities", "semantic_tags", "key_facts", "links"):
            if key in value and (
                not isinstance(value[key], list)
                or any(not isinstance(item, str) for item in value[key])
            ):
                raise MonitorDecisionError()
        if "resource_lead" in value and type(value["resource_lead"]) is not bool:
            raise MonitorDecisionError()
        items = value.get("items", [])
        if not isinstance(items, list):
            raise MonitorDecisionError()
        for item in items:
            if isinstance(item, str):
                continue
            if not isinstance(item, dict) or any(
                key in item and not isinstance(item[key], str)
                for key in ("time", "summary", "text")
            ):
                raise MonitorDecisionError()
        bodies = [value.get("digest", ""), value.get("summary", "")]
        bodies.extend(
            item if isinstance(item, str) else item.get("summary") or item.get("text", "")
            for item in items
        )
        if value["match"] and not any(body.strip() for body in bodies):
            raise MonitorDecisionError()
        return value

    def _normalize_decision(self, decision, messages=None):
        decision = self._validate_decision(decision)

        title = redact_urls_in_text(
            str(decision.get("title") or "发现关注内容").strip()
        )
        summary = redact_urls_in_text(self._normalize_digest(decision))
        topic_key = redact_urls_in_text(
            str(decision.get("topic_key") or title).strip()
        )
        raw_category = redact_urls_in_text(
            self._clean_short_text(decision.get("category"), "未分类", 80)
        )
        links = self._normalize_links(decision, summary, messages)
        has_private_record_link = self._has_wechat_record_link(decision, summary, messages)
        event_type = self._clean_short_text(decision.get("event_type"), "", 80)
        status_hint = self._clean_short_text(decision.get("status_hint"), "tracking", 80)
        resource_status = self._normalize_resource_status(decision.get("resource_status"))
        resource_lead = (
            self._truthy(decision.get("resource_lead"))
            or resource_status in {"mentioned_private", "mentioned_pending"}
            or "resource_lead" in event_type.casefold()
        )
        if resource_status == "linked" and not links and has_private_record_link:
            resource_status = "mentioned_private"
            resource_lead = True
        if resource_status == "none" and resource_lead:
            resource_status = "mentioned_pending"

        return {
            "match": bool(decision.get("match")),
            "score": self._clamp_score(decision.get("score", 0)),
            "title": title[:60] or "发现关注内容",
            "summary": summary[:1200],
            "topic_key": self._clean_topic_key(topic_key),
            "category": raw_category[:40],
            "raw_category": raw_category,
            "entities": [
                redact_urls_in_text(value)
                for value in self._normalize_list(decision.get("entities"), 16)
            ],
            "semantic_tags": [
                redact_urls_in_text(value)
                for value in self._normalize_list(decision.get("semantic_tags"), 12)
            ],
            "key_facts": [
                redact_urls_in_text(value)
                for value in self._normalize_list(decision.get("key_facts"), 20)
            ],
            "links": links,
            "event_type": event_type,
            "status_hint": status_hint,
            "resource_lead": resource_lead,
            "resource_status": resource_status,
            "lead_key": self._clean_topic_key(redact_urls_in_text(
                str(decision.get("lead_key") or topic_key)
            )),
        }

    def _normalize_digest(self, decision):
        digest = decision.get("digest")
        if isinstance(digest, str) and digest.strip():
            return digest.strip()

        items = decision.get("items")
        if isinstance(items, list):
            lines = []
            for idx, item in enumerate(items, 1):
                if isinstance(item, dict):
                    time_text = str(item.get("time") or "").strip()
                    text = str(item.get("summary") or item.get("text") or "").strip()
                    if text:
                        prefix = f"{idx}. "
                        if time_text:
                            prefix += f"【{time_text}】"
                        lines.append(prefix + text)
                else:
                    text = str(item).strip()
                    if text:
                        lines.append(f"{idx}. {text}")
            if lines:
                return "\n".join(lines)

        return str(decision.get("summary") or "").strip()

    def _parse_json(self, text):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                return {}
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}

    @staticmethod
    def _clamp_score(value):
        try:
            score = int(value)
        except (TypeError, ValueError):
            score = 0
        return max(0, min(100, score))

    @staticmethod
    def _clean_topic_key(value):
        value = value.strip()[:80]
        return value or "关注内容"

    @staticmethod
    def _clean_short_text(value, default="", limit=80):
        text = str(value or "").strip()
        return text[:limit] if text else default

    @staticmethod
    def _truthy(value):
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _normalize_resource_status(value):
        text = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "private": "mentioned_private",
            "pending": "mentioned_pending",
            "to_be_shared": "mentioned_pending",
            "not_shared_yet": "mentioned_pending",
            "file": "attached",
            "files": "attached",
            "attachment": "attached",
            "url": "linked",
            "link": "linked",
        }
        text = aliases.get(text, text)
        if text in {"attached", "linked", "mentioned_private", "mentioned_pending", "none"}:
            return text
        return "none"

    @staticmethod
    def _normalize_list(value, limit=12):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        result = []
        seen = set()
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(text[:180])
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _extract_urls_from_text(text):
        links = []
        for match in URL_RE.findall(str(text or "")):
            url = match.rstrip(".,;:!?，。；：！？、")
            if url:
                links.append(url)
        return links

    @staticmethod
    def _extract_links_from_text(text):
        return [
            url
            for url in TopicMonitor._extract_urls_from_text(text)
            if not is_wechat_record_url(url)
        ]

    @staticmethod
    def _has_wechat_record_link(decision, summary, messages=None):
        raw_links = TopicMonitor._normalize_list(decision.get("links"), 20)
        raw_links.extend(TopicMonitor._extract_urls_from_text(summary))
        for msg in messages or []:
            raw_links.extend(TopicMonitor._extract_urls_from_text(msg.get("text", "")))
        return any(is_wechat_record_url(url) for url in raw_links)

    def _normalize_links(self, decision, summary, messages=None):
        links = []
        links.extend(
            link
            for link in self._normalize_list(decision.get("links"), 20)
            if not is_wechat_record_url(link)
        )
        links.extend(self._extract_links_from_text(summary))
        for msg in messages or []:
            links.extend(self._extract_links_from_text(msg.get("text", "")))
        return self._normalize_list(links, 20)

    def _is_in_cooldown(self, state, decision):
        cooldown_min = self.config.get("monitor_cooldown_minutes", 15)
        if cooldown_min <= 0:
            return False
        if state.get("last_topic_key") != decision["topic_key"]:
            return False
        last_notified = state.get("last_notified_ts", 0)
        return self.now_func() - float(last_notified or 0) < cooldown_min * 60

    def _save_hit(self, messages, decision, *, source_batch_id=""):
        os.makedirs(self.hits_dir, exist_ok=True)
        timestamp = (
            str(source_batch_id).removeprefix("wgbatch_")[:20]
            if source_batch_id
            else datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        safe_key = re.sub(r"[^0-9A-Za-z._-]+", "_", decision["topic_key"])[:40] or "hit"
        path = os.path.join(self.hits_dir, f"{timestamp}_{safe_key}.txt")

        lines = [
            "关注推送命中",
            "=" * 40,
            f"群聊: {self.config.get('monitor_chat_display_name', '')}",
            f"时间: {messages[0].get('time_str', '')} ~ {messages[-1].get('time_str', '')}",
            f"主题: {decision['topic_key']}",
            "",
            redact_urls_in_text(decision["summary"]),
        ]

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
