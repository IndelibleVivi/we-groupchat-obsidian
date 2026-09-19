"""Regression tests: protected WeChat source reads must not run on the main thread.

These tests pin the app-side source-I/O threading contract for the affected
dialogs and chat-menu data preparation:

* source reads happen on a daemon worker thread, not the caller's thread;
* AppKit/rumps menu mutation stays on the main thread through the existing
  main-thread dispatch queue;
* duplicate chat-menu refresh requests coalesce instead of accumulating
  unbounded workers.

They use injected fake databases and explicit synchronization events so they
can fail deterministically on the old main-thread behavior without sleeps.
"""
import io
import queue
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

from app import WeGroupchatObsidianApp


class _FakeMenuItem:
    def __init__(self, title):
        self.title = title
        self.items = []

    def add(self, item):
        self.items.append(item)


class _OrderedMenu:
    """Minimal rumps-compatible ordered menu double for main-thread mutation."""

    def __init__(self, titles):
        self._titles = list(titles)
        self.mutating_thread_id = None

    def keys(self):
        return list(self._titles)

    def __contains__(self, title):
        return title in self._titles

    def __delitem__(self, title):
        self._titles.remove(title)

    def insert_after(self, anchor, item):
        self.mutating_thread_id = threading.get_ident()
        self._titles.insert(self._titles.index(anchor) + 1, item.title)

    def insert_before(self, anchor, item):
        self.mutating_thread_id = threading.get_ident()
        self._titles.insert(self._titles.index(anchor), item.title)


class _ThreadRecordingDB:
    """Fake WeChatDB that records which thread executed each source read."""

    def __init__(self):
        self._contacts = {"private-fixture@chatroom": "Synthetic Group"}
        self.read_threads = []
        self._release = threading.Event()
        self.entered = threading.Event()
        self.block_reads = False

    def _record(self):
        self.read_threads.append(threading.get_ident())
        if self.block_reads:
            self.entered.set()
            self._release.wait(timeout=5)

    def _load_contacts(self):
        self._record()

    def get_recent_sessions(self, limit=200):
        self._record()
        return [
            {
                "username": "private-fixture@chatroom",
                "name": "Synthetic Group",
                "is_group": True,
                "unread": 3,
                "summary": "",
                "timestamp": 10_000,
                "time_str": "2026-01-01 00:00",
            },
        ]

    def get_groups(self, include_unnamed=False):
        self._record()
        return [{"username": "private-fixture@chatroom", "name": "Synthetic Group"}]

    def count_messages_since(self, username, since_ts):
        self._record()
        return 2


def _make_app(db, menu_titles):
    class _MenuHost(WeGroupchatObsidianApp):
        # Shadow the rumps.App menu property so tests can inject an ordered
        # menu double without an AppKit UI session.
        menu = None

    app = _MenuHost.__new__(_MenuHost)
    app.config = {"hide_inactive_months": 0}
    app.db = db
    app.menu = _OrderedMenu(menu_titles)
    app._main_queue = queue.Queue()
    app._chat_menu_source_lock = threading.Lock()
    app._chat_menu_source_thread = None
    app._chat_menu_source_pending = False
    return app


class ChatMenuSourceThreadingTests(unittest.TestCase):
    def _menu_titles(self):
        return ["刷新群聊列表", "🔍 关键词搜索", "📋 最近总结", "⚙️ 设置"]

    def _drain_main_queue(self, app, *, wait_for_one=False):
        if wait_for_one:
            func, args = app._main_queue.get(timeout=5)
            func(*args)
        while not app._main_queue.empty():
            func, args = app._main_queue.get_nowait()
            func(*args)

    def _wait_for_worker_idle(self, app, timeout=5.0):
        """Deterministically wait for the coalescing worker to finish."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with app._chat_menu_source_lock:
                if app._chat_menu_source_thread is None:
                    return
            time.sleep(0.001)
        self.fail("chat-menu source worker did not become idle")

    def test_chat_menu_source_reads_run_off_the_calling_thread(self):
        db = _ThreadRecordingDB()
        main_thread_id = threading.get_ident()
        app = _make_app(db, self._menu_titles())

        with (
            patch("app.load_groups", return_value=[]),
            patch("app.get_summary_time", return_value=None),
            patch("app.get_bookmark", return_value=0),
        ):
            app._rebuild_chat_menu()
            self._wait_for_worker_idle(app)
            self._drain_main_queue(app)

        self.assertTrue(db.read_threads)
        for thread_id in db.read_threads:
            self.assertNotEqual(thread_id, main_thread_id)

    def test_chat_menu_mutation_happens_on_the_dispatched_main_thread(self):
        db = _ThreadRecordingDB()
        app = _make_app(db, self._menu_titles())
        main_thread_id = threading.get_ident()

        with (
            patch("app.load_groups", return_value=[]),
            patch("app.get_summary_time", return_value=None),
            patch("app.get_bookmark", return_value=0),
        ):
            app._rebuild_chat_menu()
            self._wait_for_worker_idle(app)
            self._drain_main_queue(app)

        self.assertEqual(app.menu.mutating_thread_id, main_thread_id)

    def test_chat_menu_refresh_requests_coalesce_into_one_worker(self):
        db = _ThreadRecordingDB()
        db.block_reads = True
        app = _make_app(db, self._menu_titles())

        with (
            patch("app.load_groups", return_value=[]),
            patch("app.get_summary_time", return_value=None),
            patch("app.get_bookmark", return_value=0),
        ):
            app._rebuild_chat_menu()
            self.assertTrue(db.entered.wait(timeout=5))
            first_thread = app._chat_menu_source_thread

            for _ in range(25):
                app._rebuild_chat_menu()

            # While the first worker is blocked in a source read, every
            # duplicate request must fold into the same worker.
            self.assertIs(app._chat_menu_source_thread, first_thread)
            db.block_reads = False
            db._release.set()
            self._wait_for_worker_idle(app)
            self._drain_main_queue(app)

    def test_related_dialog_source_reads_run_off_the_calling_thread(self):
        db = _ThreadRecordingDB()
        main_thread_id = threading.get_ident()

        cases = (
            "_show_drive_sync_chat_dialog",
            "_show_monitor_chat_dialog",
            "_show_add_to_group_dialog",
            "_show_search_dialog",
        )
        for entrypoint in cases:
            with self.subTest(entrypoint=entrypoint):
                db.read_threads.clear()
                app = _make_app(db, self._menu_titles())
                app._bring_to_front = lambda: None
                app._release_front = lambda: None
                app._input_dialog = lambda *a, **k: (False, "")

                with (
                    patch("app._HAS_APPKIT", False),
                    patch("app.selected_drive_sync_chats", return_value=[]),
                    patch("app.get_group_chats", return_value=[]),
                    patch("app.load_groups", return_value=[]),
                    patch("app._notify"),
                ):
                    if entrypoint == "_show_add_to_group_dialog":
                        getattr(app, entrypoint)("synthetic-group")
                    else:
                        getattr(app, entrypoint)()
                    self._drain_main_queue(app, wait_for_one=True)

                self.assertTrue(db.read_threads, entrypoint)
                for thread_id in db.read_threads:
                    self.assertNotEqual(thread_id, main_thread_id, entrypoint)

    def test_source_exception_reports_only_a_content_free_dialog(self):
        db = _ThreadRecordingDB()

        def boom(**kwargs):
            raise RuntimeError("https://private.example.test/secret/path")

        db.get_recent_sessions = boom
        app = _make_app(db, self._menu_titles())
        stderr = io.StringIO()

        with (
            patch("app._notify") as notify,
            redirect_stderr(stderr),
        ):
            app._show_monitor_chat_dialog()
            self._drain_main_queue(app, wait_for_one=True)

        notify.assert_called_once_with(
            "关注推送",
            "读取失败",
            "暂时无法读取群聊列表，请稍后重试。",
        )
        self.assertNotIn("private.example.test", str(notify.call_args))
        self.assertNotIn("private.example.test", stderr.getvalue())

    def test_applied_menu_preserves_unread_and_update_labels(self):
        db = _ThreadRecordingDB()
        app = _make_app(db, self._menu_titles())
        titles = []

        def _capture_item(title, callback=None):
            titles.append(title)
            return _FakeMenuItem(title)

        with (
            patch("app.load_groups", return_value=[]),
            # No summary yet -> unread label from the session's unread count.
            patch("app.get_summary_time", return_value=None),
            patch("app.get_bookmark", return_value=0),
            patch("app.rumps.MenuItem", side_effect=_capture_item),
        ):
            app._rebuild_chat_menu()
            self._wait_for_worker_idle(app)
            self._drain_main_queue(app)

        self.assertIn("📎 Synthetic Group (未总结 · 3条未读)", titles)

    def test_applied_group_menu_preserves_update_counts(self):
        db = _ThreadRecordingDB()
        app = _make_app(db, self._menu_titles())
        group = {"name": "Synthetic Group", "chats": ["private-fixture@chatroom"]}
        titles = []

        def _capture_item(title, callback=None):
            titles.append(title)
            return _FakeMenuItem(title)

        with (
            patch("app.load_groups", return_value=[group]),
            patch("app.get_group_summary_time", return_value=None),
            patch("app.get_summary_time", return_value=None),
            patch("app.get_bookmark", return_value=500),
            patch("app.rumps.MenuItem", side_effect=_capture_item),
        ):
            app._rebuild_chat_menu()
            self._wait_for_worker_idle(app)
            self._drain_main_queue(app)

        self.assertIn("   Synthetic Group（2条未读）", titles)

    def test_duplicate_refresh_while_running_does_exactly_one_extra_pass(self):
        db = _ThreadRecordingDB()
        db.block_reads = True
        app = _make_app(db, self._menu_titles())

        with (
            patch("app.load_groups", return_value=[]),
            patch("app.get_summary_time", return_value=None),
            patch("app.get_bookmark", return_value=0),
        ):
            app._rebuild_chat_menu()
            self.assertTrue(db.entered.wait(timeout=5))
            db.block_reads = False
            # Six duplicate requests while the worker is busy.
            for _ in range(6):
                app._rebuild_chat_menu()
            db._release.set()

            self._wait_for_worker_idle(app)
            self._drain_main_queue(app)
            # Exactly one extra pass is queued for the collapsed requests.
            self._drain_main_queue(app)

        # Two full preparations: the first pass plus one coalesced follow-up.
        self.assertEqual(len(db.read_threads), 2)


if __name__ == "__main__":
    unittest.main()
