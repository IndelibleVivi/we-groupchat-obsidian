"""Native-repository integration gates. These are not the offline harness.

All fixtures use temporary synthetic data. No provider, WeChat process,
LaunchAgent, live configuration or user's vault is accessed.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from core import wechat_db
from core.daily_digest import refresh_pending_daily_digests, write_daily_digest
from core.knowledge import KnowledgeStore
from core.monitor import TopicMonitor, load_state, save_state
from core.monitor_state import MonitorStateStore
from core.monitor_source import MonitorSourceError
from scripts import catch_up_monitor as catchup
from tests.test_monitor_source import ConflictingKnowledgeStore, CursorDB, raw_message


class RecoveryPipelineTests(unittest.TestCase):
    def test_real_digest_producer_flows_through_apply_output_and_receipt(self):
        """Never mock rebuild_projections, write_daily_digest, or receipt creation."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {
                'monitor_knowledge_db': str(root / 'knowledge.db'),
                'monitor_obsidian_root': str(root / 'vault'),
                'monitor_obsidian_subdir': 'WGO',
                'daily_digest_dir': str(root / 'digests'),
                'daily_digest_timezone': 'UTC',
            }
            store = KnowledgeStore(config['monitor_knowledge_db'], config['monitor_obsidian_root'])
            store.connect().close()
            chats = [{'username': 'fixture@chatroom', 'name': 'Synthetic group'}]
            result = {
                'complete': ['fixture@chatroom'], 'blocked': {},
                'pages': {'fixture@chatroom': 1}, 'statuses': {'no_match': 1},
                'affected_dates': ['2026-01-02'],
                'per_chat': {'fixture@chatroom': {
                    'pages': 1, 'statuses': {'no_match': 1}, 'event_ids': [],
                    'affected_dates': ['2026-01-02'], 'outcome': 'complete',
                    'blocked_reason': '',
                }},
            }
            receipts = []
            real_write = catchup.write_reconciliation_receipt
            def capture(receipt):
                path = real_write(receipt, receipts_dir=root / 'receipts')
                receipts.append(json.loads(path.read_text()))
                return path
            stdout = io.StringIO()
            lock = MagicMock()
            lock.acquire.return_value = lock
            with (
                patch.object(catchup, 'audit_pending', return_value=[{
                    'username': 'fixture@chatroom', 'name': 'Synthetic group',
                    'checkpoint': 10, 'count': 1, 'capped': False,
                }]),
                patch.object(catchup, 'launch_agent_report', return_value=(None, SimpleNamespace(loaded=False))),
                patch.object(catchup, 'AppInstanceLock', return_value=lock),
                patch.object(catchup, 'backup_runtime_state', return_value=root / 'backup'),
                patch.object(catchup, 'TopicMonitor'),
                patch('core.daily_digest.ReviewQueue.from_config', return_value=SimpleNamespace(pending=lambda: [])),
                patch.object(catchup, 'drain_monitors', return_value=result),
                patch.object(catchup, '_checkpoint_for_chat', return_value=11),
                patch.object(catchup, 'state_file_for_chat', return_value=str(root / 'fixture-state.json')),
                patch.object(catchup, 'write_reconciliation_receipt', side_effect=capture),
                contextlib.redirect_stdout(stdout),
            ):
                code = catchup.apply_catch_up(config, chats, object(), SimpleNamespace(
                    audit_limit=100, max_pages_per_chat=3, max_minutes=1,
                ))
            self.assertEqual(code, 0)
            self.assertEqual(receipts[-1]['state'], 'complete')
            self.assertIn('Digest 2026-01-02: 0 notes / 0 actions', stdout.getvalue())
            digest = receipts[-1]['projections']['digests'][0]
            self.assertEqual(digest, {'date': '2026-01-02', 'notes': 0, 'actions': 0})
            self.assertTrue((root / 'digests' / '2026-01-02-daily-digest.md').is_file())
            lock.release.assert_called_once()

    def test_actual_corrupt_zstd_row_never_advances_monitor_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'synthetic.db'
            conn = sqlite3.connect(database)
            conn.execute('CREATE TABLE Msg_fixture (local_type INTEGER, create_time INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER, status INTEGER)')
            conn.execute('INSERT INTO Msg_fixture VALUES (1, 11, ?, 4, 0)', (b'not a zstd frame',))
            conn.commit()
            conn.close()
            reader = wechat_db.WeChatDB.__new__(wechat_db.WeChatDB)
            reader._contacts = {}
            reader._nick_to_remark = {}
            reader._load_contacts = lambda: None
            source = CursorDB({'logical-fixture': ('generation-fixture', [])})
            def read_corrupt(*_args, **_kwargs):
                # Actual SQLite reader and installed zstandard decoder, not a stub.
                messages = reader._get_messages_from_paths(
                    'fixture@chatroom', [str(database)], 'Msg_fixture',
                    limit=2, page_forward=True, include_filtered=True,
                    db_shard_id='generation-fixture', after_cursor=(10, 0),
                )
                return {'messages': messages, 'next_cursor': '[11,1]', 'exhausted': True}
            source.get_cursor_page_for_shard = read_corrupt
            state_path = str(root / 'state.json')
            save_state({'last_checked_ts': 10}, state_path)
            before = Path(state_path).read_bytes()
            calls = []
            monitor = TopicMonitor(
                source, {
                    'monitor_topic': 'synthetic',
                    'monitor_chat_username': 'fixture@chatroom',
                    'monitor_chat_display_name': 'Synthetic group',
                    'monitor_context_overlap_minutes': 0,
                    'monitor_max_messages_per_run': 2,
                    'monitor_ai_retry_attempts': 0,
                }, state_file=state_path, hits_dir=str(root / 'hits'),
                ai_evaluator=lambda *_: calls.append(True) or {'match': False, 'score': 0},
                now_func=lambda: 1000,
            )
            try:
                result = monitor.check_once()
            except (MonitorSourceError, wechat_db.WeChatSourceDegraded) as exc:
                status = exc.code
            else:
                status = result['status']
            self.assertEqual(status, 'source_message_decode_failed')
            self.assertEqual(Path(state_path).read_bytes(), before)
            self.assertEqual(calls, [])

    def test_decode_failure_blocks_catchup_without_burning_page_budget(self):
        monitor = MagicMock()
        monitor.check_once.return_value = {'status': 'source_message_decode_failed'}
        result = catchup.drain_monitors(
            [({'username': 'fixture', 'name': 'Synthetic group'}, monitor)],
            max_pages_per_chat=10, max_minutes=1,
        )
        self.assertEqual(result['blocked'], {'fixture': 'source_message_decode_failed'})
        self.assertEqual(result['pages'], {})
        monitor.check_once.assert_called_once()

    def test_projection_publish_crash_repairs_without_provider_replay_or_cursor_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {
                'monitor_knowledge_db': str(root / 'knowledge.db'),
                'monitor_obsidian_root': str(root / 'vault'),
                'monitor_obsidian_subdir': 'WGO',
                'daily_digest_dir': str(root / 'digests'),
                'daily_digest_timezone': 'UTC',
                'monitor_topic': 'synthetic repair',
                'monitor_chat_username': 'fixture@chatroom',
                'monitor_chat_display_name': 'Synthetic group',
                'monitor_max_messages_per_run': 10,
                'monitor_context_overlap_minutes': 0,
                'monitor_ai_retry_attempts': 0,
                'monitor_cooldown_minutes': 0,
                'monitor_knowledge_enabled': True,
            }
            state_path = str(root / 'monitor-state.json')
            save_state({'last_checked_ts': 10}, state_path)
            state_store = MonitorStateStore(state_path)
            store = ConflictingKnowledgeStore(
                config['monitor_knowledge_db'],
                config['monitor_obsidian_root'],
                obsidian_subdir=config['monitor_obsidian_subdir'],
                state_store=state_store,
                now_func=lambda: 1767312000,
            )
            # A pre-existing page makes staleness observable after the event
            # transaction commits but before its invalidation is drained.
            stale = write_daily_digest(
                config,
                now_func=lambda: 1767315600,
                target_date='2026-01-02',
            )
            stale_bytes = Path(stale['path']).read_bytes()
            source_message = raw_message(
                'generation-fixture', 1, 1767312000,
                'synthetic committed event',
            )
            source_message['time_str'] = '2026-01-02 00:00'
            db = CursorDB({
                'logical-fixture': (
                    'generation-fixture',
                    [source_message],
                ),
            })
            decision = {
                'match': True,
                'score': 95,
                'title': 'Synthetic committed event',
                'summary': 'Canonical event committed before Digest repair.',
                'topic_key': 'projection-crash-repair',
                'category': '测试',
            }
            provider_calls = []
            monitor = TopicMonitor(
                db,
                config,
                state_file=state_path,
                hits_dir=str(root / 'hits'),
                ai_evaluator=lambda *_: provider_calls.append(True) or decision,
                knowledge_store=store,
                now_func=lambda: 1767312000,
            )

            first = monitor.check_once()
            self.assertEqual(first['status'], 'monitor_state_conflict')
            self.assertEqual(Path(stale['path']).read_bytes(), stale_bytes)
            self.assertEqual(load_state(state_path)['last_checked_ts'], 10)

            second = monitor.check_once()
            self.assertEqual(second['status'], 'duplicate')
            self.assertTrue(second['knowledge_event_reused'])
            self.assertEqual(provider_calls, [True])
            repair = refresh_pending_daily_digests(
                config,
                now_func=lambda: 1767315600,
            )
            self.assertEqual(repair['state'], 'refreshed')
            self.assertEqual(repair['written_dates'], ['2026-01-02'])
            conn = store.connect()
            try:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_digest_changes').fetchone()[0], 0)
            finally:
                conn.close()
            self.assertEqual(
                load_state(state_path)['source_cursors']['logical-fixture']['cursor_token'],
                '[1767312000,1]',
            )
            self.assertIn('Synthetic committed event', Path(stale['path']).read_text())


if __name__ == '__main__':
    unittest.main()
